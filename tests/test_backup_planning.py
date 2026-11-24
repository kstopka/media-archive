"""Read-only backup scope and target selection using synthetic volumes."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
import errno
from hashlib import sha256
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import unittest
import shutil
from unittest.mock import patch

from media_archive.backup_planning import plan_backup
from media_archive.backup_execution import _fresh_copy_status, execute_backup_trial
from media_archive.backup_snapshots import _publish, _required, distribute_snapshot
from media_archive._transfer_files import Destination, Part
from media_archive.cli import main
from media_archive.config import ConfigError, load_config
from media_archive.devices import VolumeUnavailable
from media_archive.import_restore import reconcile_imports
from media_archive.sources import LocalSource
from media_archive.state import DeviceKey, StateError, history, journal, open_database, transaction
from media_archive.state.catalog import load_locations
from media_archive.state.registry import initialize_database, load_registered_devices, register_configuration
from media_archive.state.reconciliation import load_reconciliation
from media_archive.state.snapshots import restore_snapshot, validate_snapshot
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

    def test_cli_requires_explicit_backup_mode(self) -> None:
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            with self.assertRaises(SystemExit) as error:
                main(["--config", str(self.settings), "backup"])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(main(["--config", str(self.settings), "backup", "--dry-run", "--profile", "data"]), 0)
        self.assertIn("Wszystko", output.getvalue())

    def test_cli_rejects_unverified_replacement_and_preserves_old_file(self) -> None:
        self._file("documents", "signed.pdf", b"edited")
        self._archive_file("data", "a", "documents", "signed.pdf", b"old")
        group = self.config.archive_group("data")
        archive = group.target("a").mount / group.root_relative / self.config.collection("documents").archive_relative
        command = ["backup", "--replace-test", "--profile", "data", "--collection", "documents", "--targets", "a"]

        def copy_corrupted(part, source_fd):
            Path(f"/proc/self/fd/{part.directory}/copy").write_bytes(b"broken")

        stderr = StringIO()
        with (redirect_stdout(StringIO()), redirect_stderr(stderr),
              patch("media_archive.transfer.verify_volume", side_effect=self._verify_volume),
              patch("media_archive.backup_execution.open_local_source", side_effect=self._backup_source),
              patch.object(Part, "rsync_from", copy_corrupted)):
            with self.assertRaises(SystemExit) as error:
                main(command)
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(main(["--config", str(self.settings), *command]), 3)
        self.assertEqual((archive / "signed.pdf").read_bytes(), b"old")

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
              patch("media_archive.backup_execution.open_local_source", side_effect=self._backup_source)):
            return execute_backup_trial(self.config, profile_id="data", targets=(alias,), collection_id="photos", **options)

    def _replace(self, collection_id="documents", targets=("a",)):
        with (patch("media_archive.transfer.verify_volume", side_effect=self._verify_volume),
              patch("media_archive.backup_execution.open_local_source", side_effect=self._backup_source)):
            return execute_backup_trial(self.config, profile_id="data", targets=targets, collection_id=collection_id,
                                  replace_test=True)

    def _cleanup(self, approve, targets=("a",), **options):
        with (patch("media_archive.transfer.verify_volume", side_effect=self._verify_volume),
              patch("media_archive.backup_cleanup.verify_volume", side_effect=self._verify_volume),
              patch("media_archive.backup_execution.open_local_source", side_effect=self._backup_source)):
            return execute_backup_trial(self.config, profile_id="data", targets=targets, collection_id="photos",
                                        approve_cleanup=approve, **options)

    def _parallel(self, targets=("a", "b"), *, final_probe=None, collection_id="photos"):
        with (patch("media_archive.transfer.verify_volume", side_effect=self._verify_volume),
              patch("media_archive.backup_execution.verify_volume", side_effect=final_probe or self._verify_volume),
              patch("media_archive.backup_execution.open_local_source", side_effect=self._backup_source)):
            return execute_backup_trial(self.config, profile_id="data", targets=targets, collection_id=collection_id,
                                        parallel_test=True)

    def _snapshot_backup(self, targets=("a", "b"), collection_id="documents"):
        with (patch("media_archive.transfer.verify_volume", side_effect=self._verify_volume),
              patch("media_archive.backup_execution.verify_volume", side_effect=self._verify_volume),
              patch("media_archive.backup_execution.open_local_source", side_effect=self._backup_source),
              patch("media_archive.backup_snapshots.verify_volume", side_effect=self._verify_volume)):
            return execute_backup_trial(self.config, profile_id="data", targets=targets,
                                        collection_id=collection_id, snapshot_test=True)

    def _snapshot_root(self, alias):
        return self.config.archive_group("data").target(alias).mount / self.config.app.archive_state_dir

    def _reconcile_backup(self):
        with (patch("media_archive.import_restore.verify_volume", side_effect=self._verify_volume),
              patch("media_archive.backup_restore.verify_volume", side_effect=self._verify_volume)):
            return reconcile_imports(self.config)

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_snapshot_backup_preflights_extra_recipients_and_distributes_identical_state(self) -> None:
        self.connected.add(("data", "b"))
        source = self._file("documents", "report.pdf")

        def full(destination, sizes):
            if destination.volume.root == self.config.archive_group("data").target("b").mount:
                return 0, 4096
            return _required(destination, sizes)

        with patch("media_archive.backup_snapshots._required", side_effect=full):
            blocked = self._snapshot_backup(("a",))
        self.assertEqual(blocked.code, 3, blocked.details)
        self.assertFalse(self._snapshot_root("a").exists())
        self.assertFalse((self._snapshot_root("a").parent / "Data" / "Documents").exists())

        plan = self._snapshot_backup(("a",))
        self.assertEqual(plan.code, 0, plan.details)
        self.assertEqual(plan.details["Stan kopii BACKUP"][0]["status kopii"], "1/2")
        first, second = [self._snapshot_root(row["cel"]) / row["generacja"]
                         for row in plan.details["Snapshoty BACKUP"]]
        self.assertEqual(first.name, second.name)
        self.assertEqual(validate_snapshot(first), validate_snapshot(second))
        self.assertEqual({item.name: item.read_bytes() for item in first.iterdir()},
                         {item.name: item.read_bytes() for item in second.iterdir()})
        with open_database(first / "state.sqlite3") as connection:
            location, = history.load_occurrences(connection, "local", str(source))
            self.assertEqual(len(history.load_archive_copies(connection, location.id)), 1)

    def test_snapshot_backup_rotates_only_valid_generations_after_publication(self) -> None:
        results = [self._snapshot_backup(("a",)) for _ in range(5)]
        self.assertTrue(all(result.code == 0 for result in results), results)
        root = self._snapshot_root("a")
        foreign = root / ("snapshot-" + "0" * 32)
        self._write(foreign / "keep", b"foreign")
        with patch("media_archive.backup_snapshots._sync_generation", side_effect=OSError("sync")):
            partial = self._snapshot_backup(("a",))
        self.assertEqual(partial.code, 3, partial.details)
        self.assertEqual(len(list(root.iterdir())), 6)
        last = self._snapshot_backup(("a",))
        self.assertEqual(last.code, 0, last.details)
        self.assertEqual(len(list(root.iterdir())), 6)
        first_name = results[0].details["Snapshoty BACKUP"][0]["generacja"]
        self.assertFalse((root / first_name).exists())
        self.assertEqual((foreign / "keep").read_bytes(), b"foreign")

    def test_snapshot_backup_cli_creates_generation_at_archive_root(self) -> None:
        with (redirect_stdout(StringIO()), redirect_stderr(StringIO()),
              patch("media_archive.backup_snapshots.verify_volume", side_effect=self._verify_volume)):
            code = main(["--config", str(self.settings), "backup", "--snapshot-test",
                         "--profile", "data", "--targets", "a", "--collection", "documents"])
        self.assertEqual(code, 0)
        generation, = self._snapshot_root("a").iterdir()
        self.assertEqual(generation.name, f"snapshot-{validate_snapshot(generation).generation_id}")

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_snapshot_backup_keeps_media_and_other_generation_if_one_publication_fails(self) -> None:
        self.connected.add(("data", "b"))
        self._file("documents", "report.pdf")

        def fail_b(config, key, source, generation):
            if key.alias == "b":
                raise StateError("Błąd zapisu B.")
            return _publish(config, key, source, generation)

        with patch("media_archive.backup_snapshots._publish", side_effect=fail_b):
            result = self._snapshot_backup()
        self.assertEqual(result.code, 3, result.details)
        self.assertEqual(result.details["Stan kopii BACKUP"][0]["status kopii"], "2/2")
        first, second = result.details["Snapshoty BACKUP"]
        validate_snapshot(self._snapshot_root("a") / first["generacja"])
        self.assertIn("Błąd zapisu", second["błąd"])

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_snapshot_backup_rechecks_fresh_copies_after_snapshot_publication(self) -> None:
        self.connected.add(("data", "b"))
        source = self._file("photos", "example.jpg", b"initial")

        def edit_during_snapshot(*args):
            source.write_bytes(b"changed")
            return distribute_snapshot(*args)

        with patch("media_archive.backup_execution.distribute_snapshot", side_effect=edit_during_snapshot):
            result = self._snapshot_backup(collection_id="photos")
        self.assertEqual(result.code, 3, result.details)
        self.assertEqual(result.details["Stan kopii BACKUP"][0]["status kopii"], "0/2")
        self.assertTrue(all(row["stan"] == "zweryfikowano i zapisano" for row in result.details["Snapshoty BACKUP"]))

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_restore_backup_snapshot_reconciles_later_archive_content(self) -> None:
        self.connected.add(("data", "b"))
        source = self._file("documents", "report.pdf", b"previous")
        first = self._snapshot_backup()
        self.assertEqual(first.code, 0, first.details)
        generation = self._snapshot_root("a") / first.details["Snapshoty BACKUP"][0]["generacja"]
        self._write(source, b"edited")
        later = self._snapshot_backup()
        self.assertEqual(later.code, 0, later.details)
        restore_snapshot(self.config, generation)
        result = self._reconcile_backup()
        self.assertEqual(result["Nowe treści na znanych ścieżkach BACKUP"], 2)
        with open_database(self.database) as connection:
            self.assertIsNone(load_reconciliation(connection))
            present = [row for row in load_locations(connection, None, None)
                       if row.kind == "archive" and row.present]
            self.assertEqual([row.sha256 for row in present], [sha256(b"edited").hexdigest()] * 2)
        with patch.object(Part, "rsync_from", side_effect=AssertionError("Kopie już istnieją.")):
            final = self._snapshot_backup()
        self.assertEqual(final.code, 0, final.details)
        self.assertEqual(final.details["Stan kopii BACKUP"][0]["status kopii"], "2/2")

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_restore_backup_snapshot_completes_a_published_pending_attempt(self) -> None:
        self._file("documents", "report.pdf", b"published")
        with patch("media_archive.transfer.journal.commit_result", side_effect=StateError("Błąd zapisu wyniku.")):
            partial = self._snapshot_backup(("a",))
        self.assertEqual(partial.code, 3, partial.details)
        generation = self._snapshot_root("a") / partial.details["Snapshoty BACKUP"][0]["generacja"]
        restore_snapshot(self.config, generation)
        self.connected.clear()
        with self.assertRaisesRegex(StateError, "Podłącz wolumin archiwalny"):
            self._reconcile_backup()
        with open_database(self.database) as connection:
            self.assertIsNotNone(load_reconciliation(connection))
        self.connected.add(("data", "a"))
        result = self._reconcile_backup()
        self.assertEqual(result["Zatwierdzone próby BACKUP"], 1)
        with open_database(self.database) as connection:
            self.assertEqual(journal.load_unfinished(connection), ())

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_restore_backup_snapshot_closes_superseded_replacement_with_current_working_copy(self) -> None:
        source = self._file("documents", "report.pdf", b"v1")
        self.assertEqual(self._snapshot_backup(("a",)).code, 0)
        source.write_bytes(b"v2")
        with patch("media_archive.transfer.journal.commit_result", side_effect=StateError("Błąd zapisu wyniku.")):
            partial = self._snapshot_backup(("a",))
        self.assertEqual(partial.code, 3, partial.details)
        generation = self._snapshot_root("a") / partial.details["Snapshoty BACKUP"][0]["generacja"]
        source.write_bytes(b"v3")
        latest = self._snapshot_backup(("a",))
        self.assertEqual(latest.code, 0, latest.details)
        restore_snapshot(self.config, generation)
        with open_database(self.database) as connection:
            pending, = journal.load_unfinished(connection)
        source.write_bytes(b"unrelated")
        with self.assertRaisesRegex(StateError, "wymaga sprawdzenia treści celu"):
            self._reconcile_backup()
        with open_database(self.database) as connection:
            self.assertIsNotNone(load_reconciliation(connection))
        source.write_bytes(b"v3")
        result = self._reconcile_backup()
        self.assertEqual(result["Zastąpione zamiary BACKUP"], [pending.id])
        with open_database(self.database) as connection:
            self.assertEqual(journal.load_unfinished(connection), ())
            self.assertIsNotNone(journal.load_backup_supersession(connection, pending.id))
            self.assertTrue(all(row.state != "committed" for row in journal.load_attempts(connection, pending.id)))
        source.write_bytes(b"v2")
        result = self._snapshot_backup(("a",))
        self.assertEqual(result.code, 0, result.details)
        self.assertNotEqual(result.details["Wyniki BACKUP"][0]["operacja"], pending.id)

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_restore_backup_snapshot_checks_archive_paths_verified_without_transfer(self) -> None:
        self._file("photos", "readonly.jpg", b"media")
        self.assertEqual(self._snapshot_backup(("a",), collection_id="photos").code, 0)
        self.connected.add(("data", "b"))
        self._archive_file("data", "b", "photos", "readonly.jpg", b"media")
        contents = self.settings.read_text(encoding="utf-8")
        self.settings.write_text(contents.replace("ARCHIVE_DATA_TARGET_B_STATE='active'",
                                                  "ARCHIVE_DATA_TARGET_B_STATE='read_only'"), encoding="utf-8")
        self.config = load_config(self.settings)
        result = self._snapshot_backup(collection_id="photos")
        self.assertEqual(result.code, 0, result.details)
        self.assertEqual(result.details["Stan kopii BACKUP"][0]["status kopii"], "2/2")
        group = self.config.archive_group("data")
        archive = group.target("b").mount / group.root_relative / self.config.collection("photos").archive_relative
        (archive / "readonly.jpg").unlink()
        generation = self._snapshot_root("a") / result.details["Snapshoty BACKUP"][0]["generacja"]
        restore_snapshot(self.config, generation)
        self.assertEqual(self._reconcile_backup()["Sprawdzone ścieżki BACKUP"], 2)
        with open_database(self.database) as connection:
            missing, = [row for row in load_locations(connection, None, None)
                        if row.kind == "archive" and row.alias == "b" and row.path.endswith("/readonly.jpg")]
            self.assertEqual(missing.present, 0)

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_parallel_backup_overlaps_transfers_and_reuses_their_full_reads(self) -> None:
        self.connected.add(("data", "b"))
        self._file("photos", "item.jpg")
        barrier = Barrier(2, timeout=10)
        copy = Part.rsync_from
        reads = []
        verify = Part.verified_copy

        def overlap(part, fd, **options):
            barrier.wait()
            copy(part, fd, **options)

        def read_part(part):
            reads.append(part.name)
            return verify(part)

        with (patch.object(Part, "rsync_from", overlap), patch.object(Part, "verified_copy", read_part),
              patch.object(Destination, "recover_file", autospec=True, side_effect=Destination.recover_file) as recover):
            result = self._parallel()
        self.assertEqual(result.code, 0, result.details)
        self.assertEqual(len(reads), 2)
        self.assertEqual(recover.call_count, 2)  # Both reads checked absence before transfer.
        row, = result.details["Stan kopii BACKUP"]
        self.assertEqual(row["status kopii"], "2/2")
        self.assertEqual(row["ręczne zwolnienie miejsca"], "można ręcznie zwolnić miejsce")
        self.assertEqual(len({proof["dowód"] for proof in row["kopie"]}), 2)

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_parallel_backup_reports_one_disk_and_later_completes_both(self) -> None:
        self._file("review", "item.jpg")
        first = self._parallel()
        self.assertEqual(first.code, 3, first.details)
        self.assertEqual(first.details["Stan kopii BACKUP"][0]["status kopii"], "1/2")
        self.connected.add(("data", "b"))
        copy = Part.rsync_from
        group = self.config.archive_group("data")

        def copy_second(part, fd, **options):
            self.assertTrue(Path(f"/proc/self/fd/{part.parent}").resolve().is_relative_to(group.target("b").mount))
            copy(part, fd, **options)

        with patch.object(Part, "rsync_from", copy_second):
            second = self._parallel()
        self.assertEqual(second.code, 0, second.details)
        row, = second.details["Stan kopii BACKUP"]
        self.assertEqual(row["status kopii"], "2/2")
        self.assertIn("może zaproponować usunięcie kopii", row["ręczne zwolnienie miejsca"])
        self.assertNotEqual(first.details["Stan kopii BACKUP"][0]["kopie"][0]["dowód"],
                            row["kopie"][0]["dowód"])

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_parallel_backup_finishes_healthy_target_when_other_fails(self) -> None:
        self.connected.add(("data", "b"))
        self._file("photos", "1.jpg")
        self._file("photos", "2.jpg")
        group = self.config.archive_group("data")
        copy = Part.rsync_from

        def one_failure(part, fd, **options):
            if Path(f"/proc/self/fd/{part.parent}").resolve().is_relative_to(group.target("a").mount):
                raise StateError("Symulowana awaria celu.")
            copy(part, fd, **options)

        with patch.object(Part, "rsync_from", one_failure):
            result = self._parallel()
        self.assertEqual(result.code, 3, result.details)
        self.assertEqual([row["status kopii"] for row in result.details["Stan kopii BACKUP"]], ["1/2", "1/2"])
        self.assertEqual([row["wynik"] for row in result.details["Wyniki BACKUP"] if row["cel"] == "b"],
                         ["skopiowano", "skopiowano"])

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_parallel_backup_isolates_a_pinned_conflict_after_whole_batch_capacity_check(self) -> None:
        self._file("photos", "z-conflict.jpg", b"original")
        self.assertEqual(self._parallel(("a",)).code, 0)
        self._archive_file("data", "a", "photos", "z-conflict.jpg", b"modified")
        self._file("photos", "a-new.jpg")
        self.connected.add(("data", "b"))
        group = self.config.archive_group("data")
        relative = Path(group.root_relative) / self.config.collection("photos").archive_relative
        first = group.target("a").mount / relative
        second = group.target("b").mount / relative
        space = Destination.space

        def insufficient(destination):
            if destination.volume.root == group.target("b").mount:
                return 0, 4096
            return space(destination)

        with patch.object(Destination, "space", insufficient):
            result = self._parallel()
        self.assertEqual(result.code, 3, result.details)
        self.assertTrue(any(row["brakuje"] for row in result.details["Pojemność BACKUP"] if row["cel"] == "b"))
        self.assertFalse(second.exists())

        result = self._parallel()
        self.assertEqual(result.code, 3, result.details)
        self.assertEqual((first / "z-conflict.jpg").read_bytes(), b"modified")
        self.assertFalse((first / "a-new.jpg").exists())
        self.assertEqual((second / "z-conflict.jpg").read_bytes(), b"original")
        self.assertEqual((second / "a-new.jpg").read_bytes(), b"media")
        first_results = [row for row in result.details["Wyniki BACKUP"] if row["cel"] == "a"]
        self.assertEqual(first_results[0]["wynik"], "pominięto")
        self.assertIn("inną treść", first_results[1]["błąd"])
        self.assertEqual([row["status kopii"] for row in result.details["Stan kopii BACKUP"]], ["1/2", "1/2"])

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_parallel_backup_stops_both_queues_if_a_target_runs_out_of_space(self) -> None:
        self.connected.add(("data", "b"))
        for name in ("1.jpg", "2.jpg", "3.jpg"):
            self._file("photos", name)
        group = self.config.archive_group("data")
        copy = Part.rsync_from
        second_on_a = Event()

        def fill_target(part, fd, *, cancelled):
            parent = Path(f"/proc/self/fd/{part.parent}").resolve()
            if parent.is_relative_to(group.target("b").mount):
                self.assertTrue(second_on_a.wait(10))
                raise OSError(errno.ENOSPC, "test")
            if Path(f"/proc/self/fd/{fd}").resolve().name == "2.jpg":
                second_on_a.set()
                for _ in range(100):
                    if cancelled():
                        break
                    Event().wait(0.05)
                self.assertTrue(cancelled())
            copy(part, fd, cancelled=cancelled)

        with patch.object(Part, "rsync_from", fill_target):
            result = self._parallel()
        self.assertEqual(result.code, 3, result.details)
        self.assertIn("Brak miejsca", result.message)
        self.assertEqual([row["status kopii"] for row in result.details["Stan kopii BACKUP"]],
                         ["1/2", "0/2", "0/2"])

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_parallel_backup_excludes_a_copy_that_changes_after_verification(self) -> None:
        self.connected.add(("data", "b"))
        self._file("photos", "item.jpg", b"original")

        @contextmanager
        def change_before_final_read(config, key):
            if key.alias == "a":
                self._archive_file("data", "a", "photos", "item.jpg", b"modified")
            with self._verify_volume(config, key) as volume:
                yield volume

        result = self._parallel(final_probe=change_before_final_read)
        self.assertEqual(result.code, 3, result.details)
        row, = result.details["Stan kopii BACKUP"]
        self.assertEqual(row["status kopii"], "1/2")
        self.assertEqual([proof["cel"] for proof in row["kopie"]], ["b"])

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_parallel_backup_counts_fresh_evidence_on_a_read_only_target(self) -> None:
        self.connected.add(("data", "b"))
        self._file("photos", "item.jpg")
        self._file("photos", "later.jpg")
        self.assertEqual(self._parallel().code, 0)
        group = self.config.archive_group("data")
        readonly = group.model_copy(update={"targets": (
            group.target("a"), group.target("b").model_copy(update={"state": "read_only"}),
        )})
        self.config = self.config.model_copy(update={"archive_groups": tuple(
            readonly if item.id == group.id else item for item in self.config.archive_groups
        )})
        with patch.object(Part, "rsync_from", side_effect=AssertionError("Obie kopie już istnieją.")):
            result = self._parallel()
        self.assertEqual(result.code, 0, result.details)
        self.assertEqual(result.details["Stan kopii BACKUP"][0]["status kopii"], "2/2")
        self.assertEqual([row["cel"] for row in result.details["Pojemność BACKUP"]], ["a"])
        self.assertEqual([row["wynik"] for row in result.details["Wyniki BACKUP"] if row["cel"] == "b"],
                         ["zgodna · tylko do odczytu"] * 2)
        missing = group.target("b").mount / group.root_relative / self.config.collection("photos").archive_relative / "item.jpg"
        missing.unlink()
        partial = self._parallel()
        self.assertEqual(partial.code, 3, partial.details)
        self.assertEqual([row["status kopii"] for row in partial.details["Stan kopii BACKUP"]], ["1/2", "2/2"])

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_parallel_backup_invalidates_both_copies_after_a_local_edit(self) -> None:
        self.connected.add(("data", "b"))
        file = self._file("photos", "item.jpg", b"old")

        def changed_source(*args):
            self._write(file, b"new")
            return _fresh_copy_status(*args)

        with patch("media_archive.backup_execution._fresh_copy_status", side_effect=changed_source):
            result = self._parallel()
        self.assertEqual(result.code, 3, result.details)
        row, = result.details["Stan kopii BACKUP"]
        self.assertEqual(row["status kopii"], "0/2")
        self.assertEqual(row["kopie"], [])

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_parallel_backup_detects_source_edit_during_final_archive_check(self) -> None:
        self.connected.add(("data", "b"))
        source = self._file("photos", "sample.jpg", b"original")
        attributes = LocalSource.attributes
        second = self.config.archive_group("data").target("b").mount

        def edit_during_archive_check(reader, path):
            if reader._volume.root == second:
                self._write(source, b"modified")
            return attributes(reader, path)

        def final_check(*args):
            with patch.object(LocalSource, "attributes", edit_during_archive_check):
                return _fresh_copy_status(*args)

        with patch("media_archive.backup_execution._fresh_copy_status", side_effect=final_check):
            result = self._parallel()
        self.assertEqual(result.code, 3, result.details)
        row, = result.details["Stan kopii BACKUP"]
        self.assertEqual(source.read_bytes(), b"modified")
        self.assertEqual(row["status kopii"], "0/2")
        self.assertEqual(row["ręczne zwolnienie miejsca"], "brak dwóch świeżych kopii")

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_cleanup_lists_each_disk_after_transfer_and_only_removes_authorized_paths(self) -> None:
        self.connected.add(("data", "b"))
        tracked = self._file("review", "tracked.jpg", b"tracked")
        group = self.config.archive_group("data")

        def archive(alias, collection_id, relative):
            return (group.target(alias).mount / group.root_relative
                    / self.config.collection(collection_id).archive_relative / relative)

        initial = self._cleanup(lambda _: self.fail("Brak listy do zatwierdzenia."), ("a", "b"))
        self.assertEqual(initial.code, 0, initial.details)
        tracked.unlink()
        self._archive_file("data", "a", "review", "only-a.jpg", b"a")
        self._archive_file("data", "b", "backlog", "only-b.jpg", b"b")
        self._archive_file("data", "a", "photos_main", "keep.jpg", b"retained")
        self._archive_file("data", "b", "printed", "keep.jpg", b"retained")
        self._file("backlog", "variant.jpg", b"new")
        self._archive_file("data", "a", "backlog", "variant.jpg", b"old")
        before = {(alias, collection, name) for alias, collection, name in (
            ("a", "review", "tracked.jpg"), ("a", "review", "only-a.jpg"),
            ("b", "review", "tracked.jpg"), ("b", "backlog", "only-b.jpg"),
        )}

        def approve(items):
            self.assertEqual({(item.target, item.collection_id, item.path.name) for item in items}, before)
            for alias, collection, name in before:
                self.assertTrue(archive(alias, collection, name).is_file())
            variant_name = f"variant-{sha256(b'new').hexdigest()[:8]}.jpg"
            self.assertEqual(archive("a", "backlog", variant_name).read_bytes(), b"new")
            self.assertEqual(archive("b", "backlog", "variant.jpg").read_bytes(), b"new")
            return True

        clean = self._cleanup(approve, ("a", "b"))
        self.assertEqual(clean.code, 0, clean.details)
        self.assertEqual(len(clean.details["Usunięte pliki"]), len(before))
        for alias, collection, name in before:
            self.assertFalse(archive(alias, collection, name).exists())
        self.assertEqual(archive("a", "photos_main", "keep.jpg").read_bytes(), b"retained")
        self.assertEqual(archive("b", "printed", "keep.jpg").read_bytes(), b"retained")
        self.assertEqual(archive("a", "backlog", "variant.jpg").read_bytes(), b"old")
        with open_database(self.database) as connection:
            rows = [row for row in load_locations(connection, None, None)
                    if row.kind == "archive" and row.path.endswith("/tracked.jpg")]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row.present == 0 for row in rows))

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_cleanup_decline_keeps_the_verified_transfers_and_archive_files(self) -> None:
        self._file("review", "new.jpg", b"new")
        self._archive_file("data", "a", "review", "old.jpg", b"old")
        group = self.config.archive_group("data")
        root = group.target("a").mount / group.root_relative / self.config.collection("review").archive_relative

        def decline(items):
            self.assertEqual([item.path.name for item in items], ["old.jpg"])
            self.assertEqual((root / "new.jpg").read_bytes(), b"new")
            return False

        result = self._cleanup(decline)
        self.assertEqual(result.code, 0, result.details)
        self.assertEqual(result.details["Usunięte pliki"], [])
        self.assertEqual((root / "old.jpg").read_bytes(), b"old")
        self.assertEqual((root / "new.jpg").read_bytes(), b"new")

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_cleanup_recovers_missing_archive_path_after_history_write_fails(self) -> None:
        source = self._file("review", "sample.jpg", b"archive")
        self.assertEqual(self._cleanup(lambda _: False).code, 0)
        group = self.config.archive_group("data")
        relative = Path(group.root_relative) / self.config.collection("review").archive_relative / "sample.jpg"
        archive = group.target("a").mount / relative
        source.unlink()
        with patch("media_archive.state.history.record_observation", side_effect=StateError("Błąd zapisu")):
            failed = self._cleanup(lambda _: True)
        self.assertEqual(failed.code, 3, failed.details)
        self.assertFalse(archive.exists())
        with open_database(self.database) as connection:
            archived, = history.load_occurrences(connection, "archive", relative.as_posix())
            pending, = history.load_pending_archive_removals(connection, archived.volume_id)
            self.assertEqual(pending.path, relative.as_posix())

        result = self._cleanup(lambda _: self.fail("Uzgodnienie braku nie wymaga usuwania."))
        self.assertEqual(result.code, 0, result.details)
        reconciled, = result.details["Uzgodnione zamiary usunięcia"]
        self.assertEqual(reconciled["ścieżka"], relative.as_posix())
        with open_database(self.database) as connection:
            row, = [row for row in load_locations(connection, None, None) if row.id == archived.id]
            self.assertEqual(row.present, 0)
            self.assertEqual(history.load_pending_archive_removals(connection, archived.volume_id), ())

    def test_cleanup_interrupt_before_unlink_requires_new_approval(self) -> None:
        self._archive_file("data", "a", "review", "old.jpg", b"old")
        group = self.config.archive_group("data")
        path = (group.target("a").mount / group.root_relative
                / self.config.collection("review").archive_relative / "old.jpg")
        with patch.object(Destination, "remove_verified", side_effect=KeyboardInterrupt):
            self.assertEqual(self._cleanup(lambda _: True).code, 130)

        approvals = []
        def decline(items):
            approvals.extend(items)
            return False

        result = self._cleanup(decline)
        self.assertEqual(result.code, 0, result.details)
        self.assertEqual([item.path.name for item in approvals], ["old.jpg"])
        self.assertEqual(path.read_bytes(), b"old")
        reconciled, = result.details["Uzgodnione zamiary usunięcia"]
        self.assertEqual(reconciled["stan"], "ścieżka istnieje")

    def test_cleanup_checks_archive_scope_spelling_once_per_phase(self) -> None:
        for number in range(10):
            self._archive_file("data", "a", "review", f"old-{number}.jpg", b"old")
        checked = []
        original = Destination.sibling_names

        def count(destination, name_key):
            checked.append(destination.path)
            return original(destination, name_key)

        with patch.object(Destination, "sibling_names", count):
            result = self._cleanup(lambda _: True)
        self.assertEqual(result.code, 0, result.details)
        self.assertEqual(len(result.details["Usunięte pliki"]), 10)
        group = self.config.archive_group("data")
        scope = Path(group.root_relative) / self.config.collection("review").archive_relative
        self.assertEqual(checked.count(scope), 2)
        self.assertFalse(any(path.is_relative_to(scope) and path != scope for path in checked))

    def test_cleanup_revalidates_the_whole_list_before_removing_anything(self) -> None:
        self.connected.add(("data", "b"))
        self._archive_file("data", "a", "review", "a.jpg", b"old")
        self._archive_file("data", "b", "review", "b.jpg", b"old")
        group = self.config.archive_group("data")
        relative = Path(group.root_relative) / self.config.collection("review").archive_relative
        first = group.target("a").mount / relative / "a.jpg"
        second = group.target("b").mount / relative / "b.jpg"
        local = self.config.collection("review").local_root / "b.jpg"

        def return_local_file():
            self._write(local, b"returned")

        def change_target():
            self._write(second, b"new")

        def disconnect_target():
            self.connected.remove(("data", "b"))

        for change in (return_local_file, change_target, disconnect_target):
            with self.subTest(change=change.__name__):
                def approve(items):
                    self.assertEqual(len(items), 2)
                    change()
                    return True

                result = self._cleanup(approve, ("a", "b"))
                self.assertEqual(result.code, 3, result.details)
                self.assertEqual(result.details["Usunięte pliki"], [])
                self.assertEqual(first.read_bytes(), b"old")
                self.assertTrue(second.is_file())
                local.unlink(missing_ok=True)
                self._write(second, b"old")
                self.connected.add(("data", "b"))

    def test_cleanup_stops_before_approval_if_local_collection_is_missing_or_unreadable(self) -> None:
        self._archive_file("data", "a", "review", "old.jpg", b"old")
        group = self.config.archive_group("data")
        root = group.target("a").mount / group.root_relative / self.config.collection("review").archive_relative
        local = self.config.collection("review").local_root
        local.rmdir()
        deny = lambda _: self.fail("Niepełny skan nie pozwala zatwierdzić usunięcia.")
        missing = self._cleanup(deny)
        self.assertEqual(missing.code, 3, missing.details)
        local.mkdir()
        (local / "unreadable").symlink_to(local / "missing")
        unreadable = self._cleanup(deny)
        self.assertEqual(unreadable.code, 3, unreadable.details)
        self.assertEqual((root / "old.jpg").read_bytes(), b"old")

    def test_cleanup_cli_declines_without_terminal_and_rejects_month_scope(self) -> None:
        self._archive_file("data", "a", "review", "old.jpg", b"old")
        group = self.config.archive_group("data")
        archived = (group.target("a").mount / group.root_relative
                    / self.config.collection("review").archive_relative / "old.jpg")
        args = ["--config", str(self.settings), "backup", "--cleanup-test",
                "--profile", "data", "--collection", "photos", "--targets", "a"]
        output = StringIO()
        with (redirect_stdout(output), redirect_stderr(StringIO()),
              patch("media_archive.cli.sys.stdin", StringIO("USUŃ\n")),
              patch("media_archive.backup_cleanup.verify_volume", side_effect=self._verify_volume)):
            with self.assertRaises(SystemExit) as error:
                main([*args, "--month", "2025-02"])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(main(args), 0)
        self.assertIn("old.jpg", output.getvalue())
        self.assertEqual(archived.read_bytes(), b"old")

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_replacement_uses_effective_policy_and_records_new_proof_per_disk(self) -> None:
        self.connected.add(("data", "b"))
        document = self._file("documents", "signed.pdf", b"old")
        initial = self._replace(None, ("a", "b"))
        self.assertEqual(initial.code, 0, initial.details)
        self._write(document, b"edited document")
        for collection in ("review", "reviewed", "sent", "backlog", "photos"):
            self._file(collection, "image.jpg", b"edited")
            for alias in ("a", "b"):
                self._archive_file("data", alias, collection, "image.jpg", b"old")
        plan = self._replace(None, ("a", "b"))
        self.assertEqual(plan.code, 0, plan.details)
        results = plan.details["Wyniki BACKUP"]
        self.assertEqual(len(results), 8)
        self.assertTrue(all(row["wynik"] == "zastąpiono" for row in results))
        group = self.config.archive_group("data")
        for alias in ("a", "b"):
            root = group.target(alias).mount / group.root_relative
            for collection in ("review", "reviewed", "sent", "backlog", "photos"):
                path = root / self.config.collection(collection).archive_relative / "image.jpg"
                self.assertEqual(path.read_bytes(), b"old" if collection in ("backlog", "photos") else b"edited")
                if collection in ("review", "reviewed", "sent"):
                    self.assertEqual(list(path.parent.iterdir()), [path])
            archived = root / self.config.collection("documents").archive_relative / "signed.pdf"
            self.assertEqual(archived.read_bytes(), b"edited document")
            self.assertEqual(list(archived.parent.iterdir()), [archived])
        with open_database(self.database) as connection:
            old, current = history.load_occurrences(connection, "local", str(document))
            seen = history.load_archive_copies(connection, old.id)
            fresh = history.load_archive_copies(connection, current.id)
            self.assertEqual(len({copy.device_id for copy in fresh}), 2)
            self.assertEqual({row.read_sha256 for row in fresh}, {sha256(b"edited document").hexdigest()})
            self.assertTrue({row.id for row in fresh}.isdisjoint(row.id for row in seen))

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_replacement_restarts_after_interrupt_before_publication(self) -> None:
        self._file("documents", "signed.pdf", b"edited")
        self._archive_file("data", "a", "documents", "signed.pdf", b"old")
        target = self.config.archive_group("data").target("a").mount
        with patch.object(Part, "publish", side_effect=KeyboardInterrupt):
            interrupted = self._replace()
        self.assertEqual(interrupted.code, 130, interrupted.details)
        archive = target / interrupted.copies[0].archive_path
        self.assertEqual(archive.read_bytes(), b"old")
        with open_database(self.database) as connection:
            pending, = journal.load_unfinished(connection)
            self.assertEqual(journal.load_attempts(connection, pending.id)[-1].state, "needs_review")
        resumed = self._replace()
        self.assertEqual(resumed.code, 0, resumed.details)
        self.assertEqual(resumed.details["Wyniki BACKUP"][0]["operacja"], pending.id)
        self.assertEqual(archive.read_bytes(), b"edited")

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_replacement_recovers_after_publication_without_transferring_again(self) -> None:
        source = self._file("documents", "signed.pdf", b"edited")
        self._archive_file("data", "a", "documents", "signed.pdf", b"old")
        with patch("media_archive.transfer.journal.commit_result", side_effect=KeyboardInterrupt):
            interrupted = self._replace()
        self.assertEqual(interrupted.code, 130, interrupted.details)
        with open_database(self.database) as connection:
            pending, = journal.load_unfinished(connection)
            first = journal.load_attempts(connection, pending.id)[-1]
            location, = history.load_occurrences(connection, "local", str(source))
            self.assertEqual(history.load_archive_copies(connection, location.id), ())
        with patch.object(Part, "rsync_from", side_effect=AssertionError("plik jest już opublikowany")):
            resumed = self._replace()
        self.assertEqual(resumed.code, 0, resumed.details)
        with open_database(self.database) as connection:
            evidence, = history.load_archive_copies(connection, location.id)
            self.assertNotEqual(evidence.id, first.verification_id)
            self.assertEqual(journal.load_unfinished(connection), ())

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_replacement_reconciles_previous_publication_before_new_source_version(self) -> None:
        source = self._file("documents", "signed.pdf", b"version a")
        initial = self._replace()
        self.assertEqual(initial.code, 0, initial.details)
        path = self.config.archive_group("data").target("a").mount / initial.copies[0].archive_path
        self._write(source, b"version b")
        with patch("media_archive.transfer.journal.commit_result", side_effect=KeyboardInterrupt):
            self.assertEqual(self._replace().code, 130)
        self.assertEqual(path.read_bytes(), b"version b")
        with open_database(self.database) as connection:
            pending, = journal.load_unfinished(connection)
        self._write(source, b"version c")
        self.assertEqual(self._replace().code, 0)
        self.assertEqual(path.read_bytes(), b"version c")
        with open_database(self.database) as connection:
            self.assertEqual(journal.load_unfinished(connection), ())
            last = journal.load_attempts(connection, pending.id)[-1]
            self.assertEqual(last.state, "committed")
            _, previous, _ = history.load_occurrences(connection, "local", str(source))
            proof, = history.load_archive_copies(connection, previous.id)
            self.assertEqual(proof.id, last.verification_id)
        self._write(source, b"version b")
        self.assertEqual(self._replace().code, 0)
        self.assertEqual(path.read_bytes(), b"version b")

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_replacement_refreshes_catalog_when_previous_version_is_already_present(self) -> None:
        source = self._file("documents", "signed.pdf", b"old")
        initial = self._replace()
        self.assertEqual(initial.code, 0, initial.details)
        relative = initial.copies[0].archive_path
        archive = self.config.archive_group("data").target("a").mount / relative
        self._write(source, b"edited")
        self.assertEqual(self._replace().code, 0)
        self._write(source, b"old")
        self._write(archive, b"old")

        with patch.object(Part, "rsync_from", side_effect=AssertionError("przywrócona kopia już istnieje")):
            restored = self._replace()
            repeated = self._replace()
        self.assertEqual(restored.code, 0, restored.details)
        self.assertEqual(repeated.code, 0, repeated.details)
        outcome, = restored.details["Wyniki BACKUP"]
        self.assertEqual(outcome["wynik"], "zgodna")
        self.assertEqual(repeated.details["Wyniki BACKUP"][0]["operacja"], outcome["operacja"])
        with open_database(self.database) as connection:
            rows = [row for row in load_locations(connection, None, None) if row.path == relative.as_posix()]
            present, = [row for row in rows if row.present]
            self.assertEqual(present.sha256, sha256(b"old").hexdigest())
            old, _ = history.load_occurrences(connection, "local", str(source))
            self.assertEqual(len(history.load_archive_copies(connection, old.id)), 2)

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_replacement_protects_a_changed_destination_during_publication_and_retry(self) -> None:
        self._file("documents", "signed.pdf", b"edited")
        self._archive_file("data", "a", "documents", "signed.pdf", b"old")
        publish = Part.publish

        def change_then_publish(part, destination, *args, **kwargs):
            self._write(destination.volume.root / destination.path, b"unexpected")
            publish(part, destination, *args, **kwargs)

        with patch.object(Part, "publish", change_then_publish):
            blocked = self._replace()
        self.assertEqual(blocked.code, 3, blocked.details)
        disk = self.config.archive_group("data").target("a").mount
        path = disk / blocked.copies[0].archive_path
        self.assertEqual(path.read_bytes(), b"unexpected")
        with patch.object(Part, "rsync_from", side_effect=AssertionError("zmieniony cel nie pozwala na transfer")):
            self.assertEqual(self._replace().code, 3)
        self.assertEqual(path.read_bytes(), b"unexpected")

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

        with (patch("media_archive.backup_execution.plan_backup", side_effect=plan_then_remove),
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
