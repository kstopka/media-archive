"""Exercise initial inventory and recovery using synthetic local and archive volumes."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from media_archive.cli import main
from media_archive.config import load_config
from media_archive.devices import MountedVolume, VolumeUnavailable, _mount_id
from media_archive.import_planning import plan_import
from media_archive.sources import LocalSource
from media_archive.state import DeviceKey, StateError, open_database
from media_archive.state.history import load_source_id, record_observation
from media_archive.state.initialization import load_baseline_skips, load_initialization
from media_archive.state.registry import initialize_database
from media_archive.state.reconciliation import load_reconciliation
from media_archive.state.snapshots import create_snapshot, restore_snapshot
from tests.config_fixtures import registry_values, write_config


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = root / "settings.env"
        values = registry_values(root)
        values.update(ARCHIVE_DATA_TARGET_B_STATE="read_only", APP_REPORT_KEEP_RUNS="1",
                      PHONE_MAIN_INITIAL_POLICY="baseline_skip_missing", PHONE_SECONDARY_TRANSPORT="adb",
                      PHONE_SECONDARY_SERIAL="synthetic-secondary", PHONE_SECONDARY_CAMERA_DIR="/sdcard/DCIM/Camera")
        write_config(self.settings, values)
        self.config = load_config(self.settings)
        self.config.app.state_dir.mkdir(parents=True)
        initialize_database(self.config)
        self.mounts = {DeviceKey("local", volume.alias): volume.root for volume in self.config.local.volumes}
        self.archives = {}
        for group in self.config.archive_groups:
            for target in group.targets:
                key = DeviceKey("archive", target.alias, group.id)
                self.mounts[key] = target.mount
                self.archives[key] = target.mount / group.root_relative
                self.archives[key].mkdir(parents=True)
        for collection in self.config.collections:
            collection.local_root.mkdir(parents=True, exist_ok=True)
        self.offline = set()
        self.enterContext(patch("media_archive.sources.verify_volume", side_effect=self._verify))

    @contextmanager
    def _verify(self, config, key, target):
        if key in self.offline:
            raise VolumeUnavailable("Testowy nośnik jest odłączony.")
        index = list(self.mounts).index(key)
        root = self.mounts[key]
        descriptor = os.open(root, os.O_PATH | os.O_DIRECTORY)
        try:
            yield MountedVolume(root, _mount_id(descriptor), "synthetic", f"8:{index + 1}",
                                f"8:{index}", (f"serial:synthetic-{index}",), True, descriptor)
        finally:
            os.close(descriptor)

    def _cli(self, *arguments: str, expected: int = 0) -> dict:
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = main(["--config", str(self.settings), *arguments])
        self.assertEqual(code, expected, output.getvalue())
        bundle = max(self.config.app.reports_dir.iterdir(), key=lambda path: path.stat().st_mtime_ns)
        return json.loads((bundle / "report.json").read_text(encoding="utf-8"))["szczegóły"]

    @contextmanager
    def _phone_source(self, root: Path):
        descriptor = os.open(root, os.O_PATH | os.O_DIRECTORY)
        try:
            yield LocalSource(MountedVolume(root, _mount_id(descriptor), "synthetic", "9:1",
                                            "9:0", (), True, descriptor), root)
        finally:
            os.close(descriptor)

    def _archive_copy(self, collection: str, name: str, data: bytes) -> None:
        relative = self.config.collection(collection).archive_relative
        for key, root in self.archives.items():
            if key.group_id == "data":
                path = root / relative / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

    def test_inventory_resumes_across_archives_and_records_nested_ownership(self) -> None:
        a, b, video_a, video_b = self.archives
        self.offline.update((b, video_a, video_b))
        photos = self.config.collection("photos").local_root
        child = self.config.collection("photos_secondary").local_root
        local_file = child / "present"
        local_file.write_bytes(b"same")
        (photos / "link").symlink_to(child, target_is_directory=True)
        for root in self.archives.values():
            (root / "outside-profile").write_bytes(b"same")

        with patch("media_archive.inventory.hash_source_file", side_effect=AssertionError("opened file")):
            first = self._cli("inventory", expected=3)["Zakresy spisu"]
            self.offline = {a, video_a}
            second = self._cli("inventory", expected=3)["Zakresy spisu"]
            self.offline = {a, b, video_b, DeviceKey("local", "working")}
            third = self._cli("inventory")["Zakresy spisu"]
        self.assertEqual([row["stan"] for row in first], ["ukończono"] * 4 + ["oczekuje"] * 3)
        self.assertNotIn("ID obserwacji", first[-2])
        self.assertEqual(first[:4], second[:4])
        self.assertEqual(second[:5], third[:5])
        self.assertTrue(all(row["stan"] == "ukończono" for row in third))
        self.assertIn(0, {row["pliki"] for row in third})
        with open_database(self.config.app.database_file) as connection:
            observations = connection.exec_driver_sql(
                "SELECT kind, path, collection_id, content_id, volume_id FROM main.history_occurrences"
            ).all()
            self.assertEqual(len(observations), 5)
            local, = [row for row in observations if row.kind == "local"]
            self.assertEqual((local.path, local.collection_id), (str(local_file), "photos_secondary"))
            self.assertTrue(all(row.content_id is None and row.volume_id is not None for row in observations))
            self.assertEqual(connection.exec_driver_sql(
                "SELECT count(*) FROM main.history_imports"
            ).scalar_one(), 0)
            self.assertEqual(load_initialization(connection, load_source_id(connection, "phone", "main")).status,
                             "pending")

    def test_local_hash_and_full_audit_link_distinct_occurrences_by_content(self) -> None:
        local = self.config.collection("photos_main").local_root / "picture"
        local.write_bytes(b"matching bytes")
        for key, root in self.archives.items():
            if key.group_id == "data":
                (root / "different-name").write_bytes(local.read_bytes())
        self._cli("inventory", "--hash-local")
        self._cli("audit", "--mode", "full", "--group", "data", "--targets", "a", "b")
        with open_database(self.config.app.database_file) as connection:
            rows = connection.exec_driver_sql(
                "SELECT kind, content_id FROM main.history_occurrences WHERE content_id IS NOT NULL"
            ).all()
            self.assertCountEqual([row.kind for row in rows], ["local", "archive", "archive"])
            self.assertEqual(len({row.content_id for row in rows}), 1)
            for table in ("history_imports", "history_placements", "history_archive_copies", "history_verifications"):
                self.assertEqual(connection.exec_driver_sql(f"SELECT count(*) FROM main.{table}").scalar_one(), 0)

    def test_configured_child_symlink_keeps_parent_pending_until_replaced(self) -> None:
        photos = self.config.collection("photos").local_root
        child = self.config.collection("photos_main").local_root
        outside = self.settings.parent / "outside"
        outside.mkdir()
        (outside / "not-in-scope").touch()
        child.rmdir()
        child.symlink_to(outside, target_is_directory=True)

        partial = self._cli("inventory", expected=3)["Zakresy spisu"]
        self.assertEqual(partial[0]["stan"], "oczekuje")
        self.assertNotIn("ID obserwacji", partial[0])
        self.assertEqual([error["ścieżka"] for error in partial[0]["błędy odczytu"]],
                         [child.relative_to(photos).as_posix()])
        child.unlink()
        child.mkdir()
        restored = child / "existing-file"
        restored.touch()

        completed = self._cli("inventory")["Zakresy spisu"]
        self.assertEqual(completed[0]["pliki"], 1)
        self.assertEqual(completed[1:], partial[1:])
        with open_database(self.config.app.database_file) as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT path, collection_id FROM main.history_occurrences"
            ).all(), [(str(restored), "photos_main")])

    def test_interruption_and_read_error_leave_scope_pending_until_retry(self) -> None:
        documents = self.config.collection("documents").local_root
        (documents / "preserved").touch()
        list_directory = LocalSource.list_directory

        def break_listing(error):
            def listing(source, path):
                if source._scope == documents:
                    raise error
                return list_directory(source, path)
            return listing

        with patch.object(LocalSource, "list_directory", new=break_listing(KeyboardInterrupt())):
            interrupted = self._cli("inventory", expected=130)["Zakresy spisu"]
        with patch.object(LocalSource, "list_directory", new=break_listing(StateError("Błąd testowy."))):
            partial = self._cli("inventory", expected=3)["Zakresy spisu"]
        completed = self._cli("inventory")["Zakresy spisu"]
        self.assertEqual(interrupted[0], partial[0])
        self.assertEqual(interrupted[0], completed[0])
        self.assertEqual(interrupted[1]["stan"], "oczekuje")
        self.assertEqual(partial[1]["stan"], "oczekuje")
        self.assertEqual(partial[1]["błędy odczytu"], [{"ścieżka": ".", "błąd": "Błąd testowy."}])
        self.assertEqual(completed[1]["pliki"], 1)

    def test_failed_scope_write_keeps_no_partial_observations(self) -> None:
        photos = self.config.collection("photos").local_root
        (photos / "first").touch()
        (photos / "second").write_bytes(b"two")

        def save_or_fail(*args, **kwargs):
            if kwargs["size"] == 3:
                raise StateError("Błąd zapisu testowego.")
            return record_observation(*args, **kwargs)

        with patch("media_archive.state.inventory.record_observation", side_effect=save_or_fail):
            partial = self._cli("inventory", expected=3)["Zakresy spisu"]
        self.assertEqual(partial[0]["stan"], "oczekuje")
        self.assertIn("błąd", partial[0])
        with open_database(self.config.app.database_file) as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT count(*) FROM main.history_occurrences"
            ).scalar_one(), 0)
        completed = self._cli("inventory")["Zakresy spisu"]
        self.assertEqual(completed[0]["pliki"], 2)

    def test_phone_baseline_freezes_manifest_and_waits_for_full_comparison(self) -> None:
        root = self.settings.parent / "synthetic-phone"
        root.mkdir()
        (root / "existing.jpg").write_bytes(b"archived")
        (root / "previously-selected.jpg").write_bytes(b"phone-only for this profile")
        self._archive_copy("photos_main", "archived-under-another-name.jpg", b"archived")
        self._archive_copy("photos_secondary", "other-profile.jpg", b"phone-only for this profile")
        with patch("media_archive.phone_initialization.open_adb_source", return_value=self._phone_source(root)):
            baseline = self._cli("init-phone", "main", expected=3)
        (root / "new.jpg").write_bytes(b"new after the baseline")
        self.offline.add(DeviceKey("archive", "b", "data"))
        audit = self._cli("audit", "--mode", "full", "--group", "data", "--targets", "a", "b", expected=3)
        audit_id = audit["ID audytu"]
        args = ("init-phone", "main", "--audit", audit_id)

        with patch("media_archive.phone_initialization.open_adb_source", side_effect=AssertionError("opened phone twice")):
            self._cli("inventory", expected=3)
            no_hashes = self._cli(*args, expected=3)
            self.assertNotIn("Pominięcia bazowe", no_hashes)
            self._cli("inventory", "--hash-local", expected=3)
            incomplete = self._cli(*args, expected=3)
            self.assertNotIn("Pominięcia bazowe", incomplete)
            with open_database(self.config.app.database_file) as connection:
                source_id = load_source_id(connection, "phone", "main")
                self.assertEqual(load_baseline_skips(connection, source_id), ())
            self.offline = {DeviceKey("archive", "a", "data")}
            self._cli("audit", "--resume", audit_id)
            result = self._cli(*args)
            self.assertEqual(result, self._cli("init-phone", "main"))

        self.assertEqual(result["ID spisu telefonu"], baseline["ID spisu telefonu"])
        self.assertEqual((result["Pliki bazowe"], result["Rozpoznane pliki"], result["Pominięcia bazowe"]), (2, 1, 1))
        rows = {Path(item["ścieżka"]).name: item for item in result["Decyzje spisu telefonu"]}
        self.assertEqual(len(rows["existing.jpg"]["odczyty archiwalne"]), 2)
        self.assertEqual(rows["previously-selected.jpg"]["odczyty archiwalne"], [])
        with patch("media_archive.import_planning.verify_volume",
                   side_effect=lambda config, key, **kwargs: self._verify(config, key, None)):
            with patch("media_archive.import_planning.open_adb_source", return_value=self._phone_source(root)):
                plan = plan_import(self.config, "main")
            with patch("media_archive.import_planning.open_adb_source", return_value=self._phone_source(root)):
                reimport = plan_import(self.config, "main", reimport=["previously-selected.jpg"])
        self.assertTrue(plan.ready)
        self.assertEqual({item.relative_path.name: item.decision for item in plan.items},
                         {"existing.jpg": "existing_baseline", "previously-selected.jpg": "baseline_skip",
                          "new.jpg": "copy"})
        self.assertEqual({item.decision for item in reimport.transfers}, {"copy", "reimport"})
        with open_database(self.config.app.database_file) as connection:
            source_id = load_source_id(connection, "phone", "main")
            self.assertEqual(load_initialization(connection, source_id).status, "completed")
            self.assertEqual([row.occurrence_id for row in load_baseline_skips(connection, source_id)],
                             [rows["previously-selected.jpg"]["wystąpienie"]])

    def test_restore_requires_fresh_full_archive_evidence_for_archive_only_baseline(self) -> None:
        root = self.settings.parent / "synthetic-phone"
        root.mkdir()
        (root / "existing.jpg").write_bytes(b"archived")
        self._archive_copy("photos_main", "archived.jpg", b"archived")
        with patch("media_archive.phone_initialization.open_adb_source", return_value=self._phone_source(root)):
            self._cli("init-phone", "main", expected=3)
        self._cli("inventory", "--hash-local")
        command = ("audit", "--mode", "full", "--group", "data", "--targets", "a", "b")
        old = self._cli(*command)["ID audytu"]
        self._cli("init-phone", "main", "--audit", old)
        generations = self.settings.parent / "generations"
        generations.mkdir()
        generation = create_snapshot(self.config, generations)
        restore_snapshot(self.config, generation)
        self._cli("reconcile", expected=3)
        self._cli("reconcile", "--audit", old, expected=3)
        with open_database(self.config.app.database_file) as connection:
            self.assertIsNotNone(load_reconciliation(connection))
        new = self._cli(*command)["ID audytu"]
        with patch("media_archive.import_restore.verify_volume",
                   side_effect=lambda config, key: self._verify(config, key, None)):
            outcome = self._cli("reconcile", "--audit", new)
        self.assertEqual(outcome["Pełne audyty użyte do uzgodnienia"], 1)
        with open_database(self.config.app.database_file) as connection:
            self.assertIsNone(load_reconciliation(connection))

    def test_restore_keeps_marker_until_a_recorded_local_baseline_copy_matches(self) -> None:
        root = self.settings.parent / "synthetic-phone"
        root.mkdir()
        (root / "existing.jpg").write_bytes(b"local")
        collection = self.config.collection("photos_main")
        primary = collection.local_root / "first.jpg"
        duplicate = collection.local_root / "second.jpg"
        primary.write_bytes(b"local")
        duplicate.write_bytes(b"local")
        with patch("media_archive.phone_initialization.open_adb_source", return_value=self._phone_source(root)):
            self._cli("init-phone", "main", expected=3)
        self._cli("inventory", "--hash-local")
        audit = self._cli("audit", "--mode", "full", "--group", "data", "--targets", "a", "b")
        self._cli("init-phone", "main", "--audit", audit["ID audytu"])
        generations = self.settings.parent / "generations"
        generations.mkdir()
        snapshot = create_snapshot(self.config, generations)
        primary.unlink()
        duplicate.write_bytes(b"other")
        restore_snapshot(self.config, snapshot)
        self._cli("reconcile", expected=3)
        with open_database(self.config.app.database_file) as connection:
            self.assertIsNotNone(load_reconciliation(connection))
        duplicate.write_bytes(b"local")
        with patch("media_archive.import_restore.verify_volume",
                   side_effect=lambda config, key: self._verify(config, key, None)):
            self._cli("reconcile")
        with open_database(self.config.app.database_file) as connection:
            self.assertIsNone(load_reconciliation(connection))

    def test_import_missing_keeps_archive_only_file_pending_for_local_copy(self) -> None:
        root = self.settings.parent / "synthetic-phone"
        root.mkdir()
        (root / "local.jpg").write_bytes(b"local")
        (root / "archive.jpg").write_bytes(b"archive")
        (root / "unsupported.GIF").write_bytes(b"unhandled format")
        (self.config.collection("photos_secondary").local_root / "present.jpg").write_bytes(b"local")
        self._archive_copy("photos_secondary", "archive.jpg", b"archive")
        self._cli("inventory", "--hash-local")
        audit = self._cli("audit", "--mode", "full", "--group", "data", "--targets", "a", "b")
        with patch("media_archive.phone_initialization.open_adb_source", return_value=self._phone_source(root)):
            result = self._cli("init-phone", "secondary", "--audit", audit["ID audytu"], expected=3)
        self.assertEqual((result["Rozpoznane pliki"], result["Pominięcia bazowe"], result["Braki lokalne"]), (1, 0, 1))
        self.assertEqual(result["Nieobsługiwane pliki telefonu"], ["unsupported.GIF"])
        rows = {Path(item["ścieżka"]).name: item for item in result["Decyzje spisu telefonu"]}
        self.assertEqual(len(rows["archive.jpg"]["odczyty archiwalne"]), 2)
        self.assertEqual(len(rows["local.jpg"]["lokalne wystąpienia"]), 1)
        (root / "new.jpg").write_bytes(b"after baseline")
        (root / "a-copy.jpg").write_bytes(b"archive")
        with (patch("media_archive.import_planning.verify_volume",
                    side_effect=lambda config, key, **kwargs: self._verify(config, key, None)),
              patch("media_archive.import_planning.open_adb_source", return_value=self._phone_source(root))):
            plan = plan_import(self.config, "secondary")
        self.assertEqual([item.relative_path.name for item in plan.transfers], ["archive.jpg"])
        decisions = {item.relative_path.name: item.decision for item in plan.items}
        self.assertEqual(decisions["new.jpg"], "awaiting_initialization")
        self.assertEqual(decisions["a-copy.jpg"], "awaiting_initialization")
        self.assertEqual(decisions["unsupported.GIF"], "unsupported")
        with open_database(self.config.app.database_file) as connection:
            source_id = load_source_id(connection, "phone", "secondary")
            state = load_initialization(connection, source_id)
            self.assertEqual(state.status, "in_progress")
            self.assertEqual([(scope.phase, scope.completed) for scope in state.scopes],
                             [("comparison", True), ("processing", False)])
            for table in ("history_imports", "history_placements", "history_archive_copies", "history_baseline_skips"):
                self.assertEqual(connection.exec_driver_sql(f"SELECT count(*) FROM main.{table}").scalar_one(), 0)

    def test_interrupted_phone_read_does_not_freeze_a_partial_manifest(self) -> None:
        root = self.settings.parent / "synthetic-phone"
        root.mkdir()
        (root / "example.jpg").touch()
        with (patch("media_archive.phone_initialization.open_adb_source", return_value=self._phone_source(root)),
              patch("media_archive.phone_initialization.hash_source_file", side_effect=KeyboardInterrupt)):
            result = self._cli("init-phone", "main", expected=130)
        self.assertIsNone(result["ID spisu telefonu"])
        with open_database(self.config.app.database_file) as connection:
            self.assertEqual(load_initialization(connection, load_source_id(connection, "phone", "main")).status,
                             "pending")
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM main.history_occurrences").scalar_one(), 0)
