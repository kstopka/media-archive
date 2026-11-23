"""Read-only backup scope and target selection using synthetic volumes."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import shutil
from unittest.mock import patch

from media_archive.backup_planning import plan_backup
from media_archive.backup_append import execute_append
from media_archive._transfer_files import Part
from media_archive.cli import main
from media_archive.config import ConfigError, load_config
from media_archive.devices import VolumeUnavailable
from media_archive.sources import LocalSource
from media_archive.state import DeviceKey, StateError, history, journal, open_database, transaction
from media_archive.state.registry import initialize_database, load_registered_devices, register_configuration
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
        self.physical_disks = {}
        self.enterContext(patch("media_archive.backup_planning.verify_volume", side_effect=self._verify_volume))

    @contextmanager
    def _verify_volume(self, config, key, *, writable=False):
        if key.role == "local":
            path = next(volume.root for volume in config.local.volumes if volume.alias == key.alias)
        else:
            if (key.group_id, key.alias) not in self.connected:
                raise VolumeUnavailable("Brak montowania.")
            path = config.archive_group(key.group_id).target(key.alias).mount
            path.mkdir(exist_ok=True)
        with mounted(path) as volume:
            yield replace(volume, physical_device=self.physical_disks.get(path, str(path)))

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

    def test_capacity_blocks_the_whole_batch_when_one_connected_target_is_full(self) -> None:
        self.connected.add(("data", "b"))
        self._file("documents", "changed.pdf", b"new")
        self._file("documents", "same.pdf", b"same")
        self._file("photos_main", "2025/2025.02/photo.jpg", b"new")
        for alias in ("a", "b"):
            self._archive_file("data", alias, "documents", "changed.pdf", b"old")
            self._archive_file("data", alias, "documents", "same.pdf", b"same")
            self._archive_file("data", alias, "photos_main", "2025/2025.02/photo.jpg", b"old")
        group = self.config.archive_group("data")
        before = self.database.read_bytes()
        reserve = self.config.app.min_free_bytes

        def space(destination):
            return (reserve if destination.volume.root == group.target("b").mount else 10 * reserve, 4096)

        with patch("media_archive.backup_capacity.Destination.space", autospec=True, side_effect=space):
            plan = plan_backup(self.config, profile_id="data")
        self.assertEqual(plan.code, 3)
        self.assertEqual(plan.details["Wybrane cele"], ["a", "b"])
        statuses = {item["cel"]: item["stan"] for item in plan.details["Cele BACKUP"]}
        self.assertEqual(statuses, {"a": "dostępny", "b": "dostępny"})
        a, b = plan.details["Pojemność BACKUP"]
        self.assertEqual(a["brakuje"], 0)
        self.assertGreater(b["brakuje"], 0)
        self.assertGreaterEqual(b["rezerwa"], 1024**3)
        self.assertGreater(b["snapshot"], 0)
        self.assertGreater(b["katalogi_i_części"], 0)
        self.assertEqual(b["pliki"], 2 * 4096)
        self.assertEqual(b["wymagane"], sum(b[key] for key in ("pliki", "katalogi_i_części", "snapshot", "rezerwa")))
        self.assertEqual({copy.action for copy in plan.copies}, {"verified", "replace", "conflict"})
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual((group.target("b").mount / group.root_relative / "Documents/changed.pdf").read_bytes(), b"old")

    def test_filters_old_pairs_by_device_and_keeps_the_previous_required_path(self) -> None:
        values = registry_values(self.root)
        values["ARCHIVE_DATA_TARGETS"] = "a,b,c,d"
        for alias in ("c", "d"):
            prefix = f"ARCHIVE_DATA_TARGET_{alias.upper()}"
            values[f"{prefix}_MOUNT"] = str(self.root / f"archive-{alias}")
            values[f"{prefix}_UUID"] = f"archive-test-{alias}"
            values[f"{prefix}_STATE"] = "active"
        settings = self.root / "new-pair.env"
        write_config(settings, values)
        config = load_config(settings)
        register_configuration(config)
        self.connected.add(("data", "c"))
        existing = self._file("documents", "known.pdf", b"known")
        new = self._file("documents", "new.pdf", b"new")
        devices = {item.key: item for item in load_registered_devices(self.database)}
        local_volume = devices[DeviceKey("local", config.local.volumes[0].alias)].volume_id
        a, b, c, d = (devices[DeviceKey("archive", alias, "data")].device_id for alias in "abcd")
        required = Path(config.archive_group("data").root_relative) / "Documents/required.pdf"
        with open_database(self.database, writable=True) as connection, transaction(connection):
            content = history.record_content(connection, sha256(b"known").hexdigest(), len(b"known"))
            occurrence = history.record_occurrence(connection, "local", str(existing), content_id=content,
                                                   collection_id="documents", volume_id=local_volume)
            history.assign_archive_pair(connection, occurrence, a, b, required.as_posix())
        before = self.database.read_bytes()
        plan = plan_backup(config, profile_id="data", collection_id="documents", targets=("a", "c"), pair=("c", "d"))
        self.assertEqual(plan.code, 0)
        copies = {(copy.source, copy.target): copy for copy in plan.copies}
        self.assertEqual(set(copies), {(existing, "a"), (new, "c")})
        self.assertEqual(copies[existing, "a"].archive_path, required)
        self.assertEqual(copies[existing, "a"].required_pair.devices, {a, b})
        self.assertEqual(copies[new, "c"].required_pair.devices, {c, d})
        self.assertEqual(self.database.read_bytes(), before)

    def test_rejects_archive_partitions_on_the_same_physical_disk(self) -> None:
        self.connected.add(("data", "b"))
        group = self.config.archive_group("data")
        self.physical_disks[group.target("a").mount] = "same"
        self.physical_disks[group.target("b").mount] = "same"
        with self.assertRaisesRegex(StateError, "różnych fizycznych dysk"):
            plan_backup(self.config, profile_id="data")
        self.physical_disks[group.target("b").mount] = "different"
        self.physical_disks[self.config.local.volumes[0].root] = "same"
        with self.assertRaisesRegex(StateError, "osobnych od źródeł"):
            plan_backup(self.config, profile_id="data")

    def test_checks_mounted_members_of_new_pair_even_when_only_one_is_selected(self) -> None:
        self.connected.add(("data", "b"))
        group = self.config.archive_group("data")
        self.physical_disks[group.target("a").mount] = "same"
        self.physical_disks[group.target("b").mount] = "same"
        self._file("documents", "new.pdf")
        options = dict(profile_id="data", targets=("a",))
        for pair in ((), ("a", "b")):
            with self.subTest(pair=pair):
                with self.assertRaisesRegex(StateError, "różnych fizycznych dysków"):
                    plan_backup(self.config, **options, pair=pair)
        self.connected.remove(("data", "b"))
        self.assertEqual(plan_backup(self.config, **options).code, 0)

    def test_rejects_file_directory_conflicts_in_planned_archive_paths(self) -> None:
        group = self.config.archive_group("data")
        devices = {item.key: item for item in load_registered_devices(self.database)}
        local = devices[DeviceKey("local", self.config.local.volumes[0].alias)].volume_id
        a, b = (devices[DeviceKey("archive", alias, "data")].device_id for alias in ("a", "b"))
        self._file("documents", "album/report.pdf", b"new")
        for filename in ("aaa.pdf", "zzz.pdf"):
            with self.subTest(filename=filename):
                existing = self._file("documents", filename, b"old")
                with open_database(self.database, writable=True) as connection, transaction(connection):
                    content = history.record_content(connection, sha256(b"old").hexdigest(), 3)
                    occurrence = history.record_occurrence(connection, "local", str(existing), content_id=content,
                                                           collection_id="documents", volume_id=local)
                    path = Path(group.root_relative) / "Documents/ALBUM"
                    history.assign_archive_pair(connection, occurrence, a, b, path.as_posix())
                plan = plan_backup(self.config, profile_id="data", targets=("a",), collection_id="documents")
                self.assertEqual(plan.code, 3)
                error, = plan.details["Błędy planowania"]
                self.assertIn("plik i katalog", error["błąd"])
                existing.unlink()

    @contextmanager
    def _backup_source(self, config, key, *, scope=None):
        with self._verify_volume(config, key) as volume:
            yield LocalSource(volume, scope)

    def _append(self, alias="a", **options):
        with (patch("media_archive.transfer.verify_volume", side_effect=self._verify_volume),
              patch("media_archive.backup_append.open_local_source", side_effect=self._backup_source)):
            return execute_append(self.config, profile_id="data", targets=(alias,), collection_id="photos", **options)

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_append_pins_different_names_and_reuses_verified_copies(self) -> None:
        relative = "2025/2025.02/image.jpg"
        source = self._file("photos_main", relative, b"new content")
        self._archive_file("data", "a", "photos_main", relative, b"old content")
        group = self.config.archive_group("data")
        first = self._append()
        self.assertEqual(first.code, 0, first.details)
        a, = first.copies
        self.assertEqual(a.action, "conflict")
        self.assertIn(sha256(b"new content").hexdigest()[:8], a.archive_path.name)
        self.assertEqual((group.target("a").mount / a.archive_path).read_bytes(), b"new content")
        self.assertEqual((group.target("a").mount / a.required_pair.path).read_bytes(), b"old content")

        self.connected.add(("data", "b"))
        second = self._append("b")
        self.assertEqual(second.code, 0, second.details)
        b, = second.copies
        self.assertEqual(b.archive_path, b.required_pair.path)
        self.assertEqual((group.target("b").mount / b.archive_path).read_bytes(), b"new content")
        self.assertEqual(source.read_bytes(), b"new content")
        with patch.object(Part, "rsync_from", side_effect=AssertionError("zgodna kopia nie wymaga transferu")):
            self.assertEqual(self._append().code, 0)
            self.assertEqual(self._append("b").code, 0)
        with open_database(self.database) as connection:
            location, = history.load_occurrences(connection, "local", str(source))
            self.assertEqual({item.path for item in history.load_archive_paths(connection, location.id)},
                             {a.archive_path.as_posix(), b.archive_path.as_posix()})
            self.assertEqual(len(history.load_archive_copies(connection, location.id)), 2)

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_append_requires_approval_to_copy_when_history_has_another_path(self) -> None:
        relative = "2025/2025.02/image.jpg"
        self._file("photos_main", relative, b"known")
        self._archive_file("data", "a", "photos_main", "previous/image.jpg", b"known")
        group = self.config.archive_group("data")
        root = Path(group.root_relative) / self.config.collection("photos_main").archive_relative
        old = root / "previous/image.jpg"
        target = root / relative
        episode = next(item.volume_id for item in load_registered_devices(self.database)
                       if item.key == DeviceKey("archive", "a", "data"))
        with open_database(self.database, writable=True) as connection, transaction(connection):
            content = history.record_content(connection, sha256(b"known").hexdigest(), 5)
            occurrence = history.record_occurrence(connection, "archive", old.as_posix(), content_id=content,
                                                   volume_id=episode)
            run = history.create_run(connection)
            history.record_observation(connection, occurrence, run, observed_at=datetime.now(timezone.utc), present=True)
        before = self.database.read_bytes()
        plan = self._append()
        self.assertEqual(plan.code, 3, plan.details)
        self.assertEqual(plan.copies[0].elsewhere, (old.as_posix(),))
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse((group.target("a").mount / target).exists())
        self.assertEqual(self._append(copy_if_elsewhere=True).code, 0)
        self.assertEqual((group.target("a").mount / old).read_bytes(), b"known")
        self.assertEqual((group.target("a").mount / target).read_bytes(), b"known")

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_append_recovers_published_file_before_committing_its_evidence(self) -> None:
        source = self._file("photos_main", "2025/2025.02/image.jpg", b"new")
        original = Part.publish

        def publish_then_interrupt(part, *args, **kwargs):
            original(part, *args, **kwargs)
            raise KeyboardInterrupt

        with patch.object(Part, "publish", publish_then_interrupt):
            interrupted = self._append()
        self.assertEqual(interrupted.code, 130, interrupted.details)
        with patch.object(Part, "rsync_from", side_effect=AssertionError("plik jest już opublikowany")):
            resumed = self._append()
        self.assertEqual(resumed.code, 0, resumed.details)
        with open_database(self.database) as connection:
            location, = history.load_occurrences(connection, "local", str(source))
            evidence, = history.load_archive_copies(connection, location.id)
            self.assertEqual(evidence.result, "match")

    def test_append_does_not_copy_a_verified_file_which_disappears_after_preflight(self) -> None:
        relative = "2025/2025.02/image.jpg"
        self._file("photos_main", relative)
        self._archive_file("data", "a", "photos_main", relative)
        group = self.config.archive_group("data")
        archive = group.target("a").mount / group.root_relative / self.config.collection("photos_main").archive_relative

        def plan_then_remove(*args, **kwargs):
            plan = plan_backup(*args, **kwargs)
            self.assertEqual(plan.copies[0].action, "verified")
            (archive / relative).unlink()
            return plan

        with (patch("media_archive.backup_append.plan_backup", side_effect=plan_then_remove),
              patch.object(Part, "create", side_effect=AssertionError("transfer was not budgeted"))):
            result = self._append()
        self.assertEqual(result.code, 3)
        self.assertIn("Odśwież plan", result.details["Wyniki BACKUP"][0]["błąd"])
        self.assertFalse((archive / relative).exists())

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_append_finishes_latest_operation_after_interrupted_recreation(self) -> None:
        source = self._file("photos_main", "2025/2025.02/image.jpg", b"new")
        first = self._append()
        self.assertEqual(first.code, 0, first.details)
        previous = first.details["Wyniki BACKUP"][0]["operacja"]
        mount = self.config.archive_group("data").target("a").mount
        published = mount / first.copies[0].archive_path
        published.unlink()
        original_publish = Part.publish

        def publish_then_interrupt(part, *args, **kwargs):
            original_publish(part, *args, **kwargs)
            raise KeyboardInterrupt

        with patch.object(Part, "publish", publish_then_interrupt):
            self.assertEqual(self._append().code, 130)
        with open_database(self.database) as connection:
            pending, = journal.load_unfinished(connection)
        self.assertNotEqual(pending.id, previous)
        with patch.object(Part, "rsync_from", side_effect=AssertionError("plik jest już opublikowany")):
            for _ in range(2):
                resumed = self._append()
                self.assertEqual(resumed.code, 0, resumed.details)
                self.assertEqual(resumed.details["Wyniki BACKUP"][0]["operacja"], pending.id)
        with open_database(self.database) as connection:
            self.assertEqual(journal.load_unfinished(connection), ())
            location, = history.load_occurrences(connection, "local", str(source))
            self.assertEqual(len(history.load_archive_copies(connection, location.id)), 2)
        self.assertEqual(published.read_bytes(), b"new")

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_append_extends_suffix_reserved_by_another_file_in_the_batch(self) -> None:
        folder = Path("2025/2025.02")
        content = b"new"
        digest = sha256(content).hexdigest()
        relative = folder / "image.jpg"
        short_name = folder / f"IMAGE-{digest[:8]}.JPG"
        source = self._file("photos_main", relative, content)
        separate = self._file("photos_main", short_name, b"other")
        self._archive_file("data", "a", "photos_main", relative, b"old")
        result = self._append()
        self.assertEqual(result.code, 0, result.details)
        copies = {copy.source: copy for copy in result.copies}
        mount = self.config.archive_group("data").target("a").mount
        self.assertEqual(copies[source].archive_path.name, f"image-{digest[:12]}.jpg")
        self.assertEqual(copies[separate].archive_path.name, short_name.name)
        self.assertEqual((mount / copies[source].required_pair.path).read_bytes(), b"old")
        self.assertEqual((mount / copies[source].archive_path).read_bytes(), content)
        self.assertEqual((mount / copies[separate].archive_path).read_bytes(), b"other")
