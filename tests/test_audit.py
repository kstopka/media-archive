"""Exercise quick AUDIT through the CLI with temporary archive trees."""

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
from media_archive.sources import LocalSource, hash_source_file
from media_archive.state import StateError, open_database
from media_archive.state.audit import save_checkpoint
from media_archive.state.history import record_observation
from media_archive.state.registry import initialize_database
from tests.config_fixtures import registry_values, write_config


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = self.root / "settings.env"
        self.values = registry_values(self.root)
        self.values.update(ARCHIVE_DATA_TARGET_B_STATE="read_only", APP_REPORT_KEEP_RUNS="1")
        write_config(self.settings, self.values)
        self.config = load_config(self.settings)
        self.config.app.state_dir.mkdir(parents=True)
        initialize_database(self.config)
        self.group = self.config.archive_group("data")
        self.mounts = {target.alias: target.mount for target in self.group.targets}
        self.archives = {alias: mount / self.group.root_relative for alias, mount in self.mounts.items()}
        for path in self.archives.values():
            path.mkdir(parents=True)
        self.offline = set()
        self.physical_devices = {"a": "8:0", "b": "8:16"}
        self.hardware = {"a": ("serial:synthetic-a",), "b": ("serial:synthetic-b",)}
        self.enterContext(patch("media_archive.sources.verify_volume", side_effect=self._verify))

    @contextmanager
    def _verify(self, config, key, target):
        self.assertEqual(key.group_id, self.group.id)
        self.assertEqual(key.role, "archive")
        if key.alias in self.offline:
            raise VolumeUnavailable("Testowy nośnik jest odłączony.")
        mount = self.mounts[key.alias]
        descriptor = os.open(mount, os.O_PATH | os.O_DIRECTORY)
        try:
            yield MountedVolume(mount, _mount_id(descriptor), "synthetic", "8:1",
                                self.physical_devices[key.alias], self.hardware[key.alias], key.alias == "b", descriptor)
        finally:
            os.close(descriptor)

    def _run(self, *arguments: str, expected: int = 0, selected: tuple[str, str] = ("a", "b")) -> str:
        return self._cli("--group", "data", "--targets", *selected, *arguments, expected=expected)

    def _cli(self, *arguments: str, expected: int = 0) -> str:
        out, error = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            code = main(["--config", str(self.settings), "audit", *arguments])
        self.assertEqual(code, expected, out.getvalue() + error.getvalue())
        return out.getvalue() + error.getvalue()

    def _report(self) -> tuple[Path, dict]:
        bundle, = self.config.app.reports_dir.iterdir()
        payload = json.loads((bundle / "report.json").read_text(encoding="utf-8"))
        self.assertTrue((bundle / "report.txt").is_file())
        return bundle, payload

    def test_quick_audit_records_presence_without_hashing_and_reports_all_differences(self) -> None:
        a, b = self.archives.values()
        # This directory is inside the group root without belonging to a backup profile.
        for root, content in ((a, b"aa"), (b, b"bb")):
            (root / "unmanaged").mkdir()
            (root / "unmanaged" / "same-size").write_bytes(content)
        (a / "only-a").touch()
        (b / "only-b").touch()
        (a / "different-size").write_bytes(b"a")
        (b / "different-size").write_bytes(b"bb")
        (a / "different-type").mkdir()
        (b / "different-type").touch()
        (a / "Photo").touch()
        (b / "photo").touch()
        (self.mounts["a"] / "outside-audit").touch()

        with patch("media_archive.sources.open_media_file", side_effect=AssertionError("opened media")):
            self._run(expected=4)
        bundle, report = self._report()
        details = report["szczegóły"]
        self.assertEqual(report["kod"], 4)
        self.assertEqual({item["rodzaj"] for item in details["Różnice"]}, {
            "tylko pierwszy nośnik", "tylko drugi nośnik", "inny rozmiar", "inny typ", "kolizja nazw",
        })
        self.assertEqual(details["Weryfikacja treści"], "nie wykonano SHA-256")
        self.assertEqual(details["Zgodne wpisy"], 2)
        with open_database(self.config.app.database_file) as connection:
            observations = connection.exec_driver_sql(
                "SELECT o.path, o.content_id, o.volume_id, v.present FROM history_occurrences o "
                "JOIN history_observations v ON v.occurrence_id = o.id WHERE v.run_id = ?",
                (details["ID obserwacji"],),
            ).all()
            self.assertEqual(len(observations), 9)
            self.assertTrue(all(content is None and volume and present for _, content, volume, present in observations))
            self.assertEqual({row[0] for row in observations if row[0].endswith("/same-size")},
                             {f"{self.group.root_relative}/unmanaged/same-size"})
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM history_verifications").scalar_one(), 0)
        self._run(expected=4)
        self.assertFalse(bundle.exists())

    def test_offline_target_is_not_an_empty_archive(self) -> None:
        self.offline.add("b")
        (self.archives["a"] / "present").touch()
        self._run(expected=3)
        _, report = self._report()
        details = report["szczegóły"]
        self.assertEqual(details["Różnice"], [])
        self.assertEqual(details["Błędy skanowania"][0]["ścieżka"], ".")
        self.assertEqual([item["pełny skan"] for item in details["Nośniki"]], [True, False])
        with open_database(self.config.app.database_file) as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM history_observations").scalar_one(), 1)

    def test_failed_directory_suppresses_absence_only_in_its_scope(self) -> None:
        a, b = self.archives.values()
        for root in (a, b):
            (root / "blocked").mkdir()
        (a / "blocked" / "unknown").touch()
        (a / "known-absent").touch()
        list_directory = LocalSource.list_directory

        def fail_subtree(source, path):
            if source.physical_device == self.physical_devices["b"] and path == Path("blocked"):
                raise StateError("Testowy katalog jest niedostępny.")
            return list_directory(source, path)

        with patch.object(LocalSource, "list_directory", new=fail_subtree):
            self._run(expected=3)
        _, report = self._report()
        details = report["szczegóły"]
        self.assertEqual([item["ścieżka"] for item in details["Różnice"]], ["known-absent"])
        self.assertEqual(details["Błędy skanowania"][0]["ścieżka"], "blocked")

    def test_interrupt_reports_completed_and_pending_directories(self) -> None:
        a, b = self.archives.values()
        for name in ("queued", "scanning"):
            for root in (a, b):
                (root / name).mkdir()
            (a / name / "unknown").touch()
        (a / "known-absent").touch()
        list_directory = LocalSource.list_directory

        def interrupt_second_side(source, path):
            if source.physical_device == self.physical_devices["b"] and path == Path("scanning"):
                raise KeyboardInterrupt
            return list_directory(source, path)

        with patch.object(LocalSource, "list_directory", new=interrupt_second_side):
            self._run(expected=130)
        bundle, report = self._report()
        self.assertTrue(bundle.name.startswith("interrupted-"))
        details = report["szczegóły"]
        self.assertEqual([item["ścieżka"] for item in details["Różnice"]], ["known-absent"])
        self.assertEqual({(item["nośnik"], item["ścieżka"]) for item in details["Błędy skanowania"]},
                         {("b", "queued"), ("b", "scanning")})
        self.assertEqual({(item["nośnik"], item["ścieżka"]) for item in details["Odczytane katalogi"]},
                         {("a", "."), ("a", "queued"), ("a", "scanning"), ("b", ".")})
        self.assertIsNone(details["ID obserwacji"])
        with open_database(self.config.app.database_file) as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM history_runs").scalar_one(), 0)

    def test_observation_write_failure_rolls_back_and_preserves_scan_report(self) -> None:
        a, b = self.archives.values()
        (a / "only-a").touch()
        (b / "only-b").touch()
        writes = 0

        def fail_after_first_observation(*args, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise StateError("Symulowany błąd zapisu obserwacji.")
            return record_observation(*args, **kwargs)

        with patch("media_archive.audit.record_observation", side_effect=fail_after_first_observation):
            self._run(expected=3)
        self.assertEqual(writes, 2)
        _, report = self._report()
        details = report["szczegóły"]
        self.assertIsNone(details["ID obserwacji"])
        self.assertIn("Błąd zapisu obserwacji", details)
        self.assertEqual([item["pełny skan"] for item in details["Nośniki"]], [True, True])
        self.assertEqual({item["ścieżka"] for item in details["Różnice"]}, {"only-a", "only-b"})
        with open_database(self.config.app.database_file) as connection:
            for table in ("history_runs", "history_occurrences", "history_observations"):
                self.assertEqual(connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one(), 0)

    def test_dry_run_preserves_state_and_skips_symlink_targets(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "outside-file").touch()
        for root in self.archives.values():
            (root / "link").symlink_to(outside, target_is_directory=True)
        database = self.config.app.database_file
        before = database.read_bytes()
        files = set(database.parent.iterdir())
        output = self._run("--dry-run")
        self.assertIn("Zgodne wpisy · 1", output)
        self.assertNotIn("outside-file", output)
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(set(database.parent.iterdir()), files)
        self.assertFalse(self.config.app.reports_dir.exists())

    def test_rejects_duplicate_selection_and_partitions_of_one_disk(self) -> None:
        self._run(expected=2, selected=("a", "a"))
        self.physical_devices["b"] = self.physical_devices["a"]
        self.assertIn("różnych fizycznych", self._run(expected=1))
        with open_database(self.config.app.database_file) as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM history_runs").scalar_one(), 0)

    def test_full_audit_compares_same_size_files_with_disks_attached_sequentially(self) -> None:
        a, b = self.archives.values()
        for root in (a, b):
            (root / "same").write_bytes(b"same bytes")
        (a / "different").write_bytes(b"old")
        (b / "different").write_bytes(b"new")
        self.offline.add("b")
        self.assertIn("do sprawdzenia 2 plików", self._run("--mode", "full", expected=3))
        _, report = self._report()
        details = report["szczegóły"]
        audit_id = details["ID audytu"]
        self.assertEqual([target["pełny skan"] for target in details["Nośniki"]], [True, False])
        self.assertEqual(details["Różnice"], [])
        original_reads = details["Odczyty SHA-256"]

        self.offline = {"a"}
        self._cli("--resume", audit_id, expected=4)
        _, report = self._report()
        details = report["szczegóły"]
        self.assertEqual(details["ID audytu"], audit_id)
        self.assertEqual(details["Zgodne pliki SHA-256"], 1)
        self.assertEqual(details["Różnice"], [{"rodzaj": "inna treść", "ścieżka": "different"}])
        self.assertEqual([item for item in details["Odczyty SHA-256"] if item["nośnik"] == "a"], original_reads)
        with open_database(self.config.app.database_file) as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT count(*) FROM history_observations WHERE run_id = ?", (audit_id,),
            ).scalar_one(), 4)
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM history_verifications").scalar_one(), 0)

    def test_full_audit_resumes_after_interrupt_and_rehashes_changed_file_only(self) -> None:
        self.offline.add("b")
        a = self.archives["a"]
        for name in ("changed", "kept", "pending"):
            (a / name).write_bytes(b"old")

        def interrupt(source, path, **kwargs):
            if path == Path("pending"):
                raise KeyboardInterrupt
            return hash_source_file(source, path, **kwargs)

        with patch("media_archive.full_audit.hash_source_file", side_effect=interrupt):
            self._run("--mode", "full", expected=130)
        _, report = self._report()
        details = report["szczegóły"]
        audit_id = details["ID audytu"]
        self.assertEqual({item["ścieżka"] for item in details["Odczyty SHA-256"]}, {"changed", "kept"})
        self.assertEqual(details["Błędy skanowania"], [])
        self.assertEqual(details["Nośniki"][0]["pliki do odczytu"], 1)
        self.assertIn("uwaga", details["Nośniki"][1])
        (a / "changed").write_bytes(b"new bytes")

        with patch("media_archive.full_audit.hash_source_file", wraps=hash_source_file) as reads:
            self._cli("--resume", audit_id, expected=3)
        self.assertEqual([call.args[1] for call in reads.call_args_list], [Path("changed"), Path("pending")])
        with open_database(self.config.app.database_file) as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM audit_checkpoints").scalar_one(), 3)
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM history_observations").scalar_one(), 4)

    def test_full_audit_rechecks_checkpoint_after_listing(self) -> None:
        a, b = self.archives.values()
        for root in (a, b):
            (root / "file").write_bytes(b"old")
        self._run("--mode", "full")
        _, report = self._report()
        audit_id = report["szczegóły"]["ID audytu"]
        list_directory = LocalSource.list_directory

        def change_after_listing(source, path):
            entries = list_directory(source, path)
            if source.physical_device == self.physical_devices["a"]:
                # Replacement guarantees a different inode even when timestamps are coarse.
                replacement = a / "replacement"
                replacement.write_bytes(b"new")
                replacement.replace(a / "file")
            return entries

        with patch.object(LocalSource, "list_directory", new=change_after_listing):
            self._cli("--resume", audit_id, expected=4)
        _, report = self._report()
        details = report["szczegóły"]
        self.assertEqual(details["Zgodne pliki SHA-256"], 0)
        self.assertEqual(details["Różnice"], [{"rodzaj": "inna treść", "ścieżka": "file"}])

    def test_full_audit_write_failure_reports_id_and_saved_progress(self) -> None:
        for root in self.archives.values():
            for name in ("first", "second"):
                (root / name).touch()

        def save_or_fail(connection, run_id, position, volume_id, root, path, *evidence):
            if path == Path("second"):
                raise StateError("Symulowany błąd zapisu punktu wznowienia.")
            return save_checkpoint(connection, run_id, position, volume_id, root, path, *evidence)

        with patch("media_archive.full_audit.save_checkpoint", side_effect=save_or_fail):
            self._run("--mode", "full", expected=3)
        _, report = self._report()
        details = report["szczegóły"]
        self.assertIn("Symulowany błąd", details["Błąd zapisu stanu"])
        self.assertEqual([item["ścieżka"] for item in details["Odczyty SHA-256"]], ["first"])
        self.assertEqual(details["Błędy skanowania"], [])
        self.assertEqual(details["Różnice"], [])
        self.assertEqual(details["Nośniki"][0]["pliki do odczytu"], 1)
        self.assertEqual(details["Nośniki"][1]["katalogi do przeskanowania"], 1)
        with patch("media_archive.full_audit.hash_source_file", wraps=hash_source_file) as reads:
            self._cli("--resume", details["ID audytu"])
        self.assertEqual([call.args[1] for call in reads.call_args_list],
                         [Path("second"), Path("first"), Path("second")])

    def test_full_audit_requires_disk_independence_and_rechecks_missing_paths(self) -> None:
        self.hardware = {"a": (), "b": ()}
        a, b = self.archives.values()
        for root in (a, b):
            (root / "file").write_bytes(b"same")
        self.offline = {"b"}
        self._run("--mode", "full", expected=3)
        _, report = self._report()
        audit_id = report["szczegóły"]["ID audytu"]
        self.offline = {"a"}
        self.assertIn("Nie potwierdzono odrębności", self._cli("--resume", audit_id, expected=3))
        self.offline.clear()
        self._cli("--resume", audit_id)

        (a / "file").unlink()
        self._cli("--resume", audit_id, expected=4)
        _, report = self._report()
        self.assertEqual(report["szczegóły"]["Różnice"], [{"rodzaj": "tylko drugi nośnik", "ścieżka": "file"}])
        with open_database(self.config.app.database_file) as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM audit_checkpoints").scalar_one(), 1)

    def test_full_audit_read_failure_invalidates_a_previous_hash(self) -> None:
        a, b = self.archives.values()
        for root in (a, b):
            (root / "file").write_bytes(b"same")
        self._run("--mode", "full")
        _, report = self._report()
        audit_id = report["szczegóły"]["ID audytu"]
        (a / "file").write_bytes(b"changed")

        def fail_read(source, path, **kwargs):
            if source.physical_device == self.physical_devices["a"]:
                raise StateError("Symulowany błąd odczytu pliku.")
            return hash_source_file(source, path, **kwargs)

        with patch("media_archive.full_audit.hash_source_file", side_effect=fail_read):
            self._cli("--resume", audit_id, expected=3)
        _, report = self._report()
        details = report["szczegóły"]
        self.assertEqual(details["Zgodne pliki SHA-256"], 0)
        self.assertEqual([item["nośnik"] for item in details["Odczyty SHA-256"]], ["b"])
        self.assertEqual(details["Błędy skanowania"][0]["ścieżka"], "file")
        with open_database(self.config.app.database_file) as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT position FROM audit_checkpoints").scalar_one(), 2)
