"""Read-only backup scope and target selection using synthetic volumes."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from media_archive.backup_planning import plan_backup
from media_archive.cli import main
from media_archive.config import ConfigError, load_config
from media_archive.devices import VolumeUnavailable
from media_archive.state.registry import initialize_database
from tests.config_fixtures import registry_values, write_config
from tests.test_import_planning import mounted


class BackupPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = self.root / "backup.env"
        values = registry_values(self.root)
        values.update({
            "COLLECTIONS": values["COLLECTIONS"] + ",finished",
            "COLLECTION_FINISHED_LOCAL_ROOT": str(self.root / "working-volume" / "Finished"),
            "COLLECTION_FINISHED_ARCHIVE_RELATIVE": "Finished",
            "COLLECTION_FINISHED_POLICY": "append",
            "BACKUP_VIDEO_COLLECTIONS": values["BACKUP_VIDEO_COLLECTIONS"] + ",finished",
        })
        write_config(self.settings, values)
        self.config = load_config(self.settings)
        self.database = self.config.app.database_file
        self.database.parent.mkdir(parents=True)
        initialize_database(self.config)
        for collection in self.config.collections:
            collection.local_root.mkdir(parents=True, exist_ok=True)
        self.connected = {("data", "a")}
        self.enterContext(patch("media_archive.backup_planning.verify_volume", side_effect=self._verify_volume))

    def _verify_volume(self, config, key, *, writable=False):
        if key.role == "local":
            return mounted(next(volume.root for volume in config.local.volumes if volume.alias == key.alias))
        self.assertTrue(writable)
        if (key.group_id, key.alias) not in self.connected:
            raise VolumeUnavailable("Brak montowania.")
        target = config.archive_group(key.group_id).target(key.alias)
        target.mount.mkdir(exist_ok=True)
        return mounted(target.mount)

    def _file(self, collection, relative, content=b"media"):
        path = self.config.collection(collection).local_root / relative
        self._write(path, content)
        return path

    def _archive_file(self, group_id, alias, collection, relative, content=b"media"):
        group = self.config.archive_group(group_id)
        path = group.target(alias).mount / group.root_relative / self.config.collection(collection).archive_relative / relative
        self._write(path, content)

    def _write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def test_profile_all_includes_helpers_and_explicit_child_policies(self) -> None:
        secondary = self._file("photos_secondary", "2025/2025.02/photo.jpg")
        primary = self._file("photos_main", "2025/2025.02/photo.jpg")
        helper = self._file("photos", "notes.xmp")
        review = self._file("review", "photo.jpg")
        document = self._file("documents", "signed.pdf")
        self._file("gopro", "Różne 2025/2025-02-01/HERO8 Black 1/clip.mp4")
        original = self.database.read_bytes()
        plan = plan_backup(self.config)
        self.assertEqual(plan.code, 0)
        self.assertEqual(plan.details["Profil BACKUP"], "data")
        rows = {row.path: row for row in plan.files}
        self.assertEqual(set(rows), {primary, secondary, helper, review, document})
        self.assertEqual(len({row.archive_path for row in plan.files}), 5)
        self.assertEqual((rows[review].changes, rows[review].deletions, rows[review].allow_delete),
                         ("replace_verified", "confirm_each_run", True))
        self.assertFalse(rows[document].allow_delete)
        self.assertEqual(self.database.read_bytes(), original)
        self.assertFalse(self.config.app.reports_dir.exists())
        self.assertEqual(tuple(self.config.archive_group("data").target("a").mount.iterdir()), ())

    def test_months_depend_on_selected_disks_and_compare_equal_size_contents(self) -> None:
        feb = "2025/2025.02/photo.jpg"
        jan = "2025/2025.01/photo.jpg"
        self._file("photos_main", feb)
        self._file("photos_secondary", feb)
        self._file("photos_secondary", jan)
        self._file("review", feb)
        self._archive_file("data", "a", "photos_main", feb)
        self._archive_file("data", "a", "photos_secondary", feb)
        self._archive_file("data", "a", "photos_secondary", jan, b"other")
        arguments = dict(profile_id="data", collection_id="photos")
        first = plan_backup(self.config, **arguments, targets=("a",))
        self.assertEqual(first.details["Zakresy zbioru"], ["Wszystko", "2025-01"])
        self.connected.add(("data", "b"))
        plan = plan_backup(self.config, **arguments, targets=("a", "b"))
        self.assertEqual(plan.details["Zakresy zbioru"], ["Wszystko", "2025-02", "2025-01"])

        exact = plan_backup(self.config, profile_id="data", targets=("b",), month="2025-02")
        self.assertEqual({row.collection_id for row in exact.files}, {"photos_main", "photos_secondary"})
        self.assertFalse(any(row.allow_delete for row in exact.files))
        with self.assertRaises(ConfigError):
            plan_backup(self.config, profile_id="data", collection_id="documents", month="2025-02")

    def test_multiple_profiles_require_selection_and_camera_month_uses_day_folders(self) -> None:
        self.connected.add(("video", "a"))
        self._file("gopro", "Różne 2025/2025-02-01/HERO8 Black 1/clip.mp4")
        self._file("gopro", "Różne 2025/2025-02-02/HERO8 Black 1/clip.mp4")
        self._file("gopro", "Różne 2025/2025-03-31/HERO8 Black 1/clip.mp4")
        self._file("finished", "2025/2025.02/export.mp4")
        proposals = plan_backup(self.config)
        self.assertEqual(proposals.details["Profile BACKUP"], ["data", "video"])
        self.assertEqual(proposals.files, ())
        plan = plan_backup(self.config, profile_id="video", month="2025-02")
        self.assertEqual(len(plan.files), 2)
        self.assertEqual({row.collection_id for row in plan.files}, {"gopro"})
        all_video = plan_backup(self.config, profile_id="video")
        self.assertEqual({row.collection_id for row in all_video.files}, {"gopro", "finished"})

    def test_month_read_errors_are_visible_and_unavailable_targets_are_not_replaced(self) -> None:
        scope = self.config.collection("photos_main").local_root
        path = scope / "2025"
        path.write_bytes(b"not a directory")
        results = plan_backup(self.config, profile_id="data", collection_id="photos", month="2025-02")
        self.assertEqual(results.code, 3)
        self.assertTrue(results.details["Błędy planowania"])
        unavailable = plan_backup(self.config, profile_id="data", targets=("a", "b"))
        self.assertEqual(unavailable.code, 3)
        self.assertEqual(unavailable.files, ())
        self.assertIn("odłączony", [row["stan"] for row in unavailable.details["Cele BACKUP"]])

    def test_cli_only_accepts_dry_run(self) -> None:
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            with self.assertRaises(SystemExit) as error:
                main(["--config", str(self.settings), "backup"])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(main(["--config", str(self.settings), "backup", "--dry-run", "--profile", "data"]), 0)
        self.assertIn("Wszystko", output.getvalue())

    def test_month_menu_and_explicit_month_report_blocked_date_directories(self) -> None:
        self.connected.add(("video", "a"))
        for group, root, layout, directory in (
            ("data", "photos", "photos_main", "2025"),
            ("data", "photos", "photos_secondary", "2025/2025.02"),
            ("video", "gopro", "gopro", "Różne 2025/2025-02-01"),
        ):
            with self.subTest(directory=directory):
                blocked = self._file(layout, directory)
                options = dict(profile_id=group, collection_id=root, targets=("a",))
                menu = plan_backup(self.config, **options)
                month = plan_backup(self.config, **options, month="2025-02")
                self.assertEqual((menu.code, month.code), (3, 3))
                error, = menu.details["Błędy planowania"]
                self.assertEqual(error["ścieżka"], str(blocked))
                self.assertEqual(month.details["Błędy planowania"], [error])
                self.assertEqual(menu.details["Zakresy zbioru"], ["Wszystko"])
                blocked.unlink()
