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
from media_archive.sources import LocalSource
from media_archive.state import DeviceKey, StateError, open_database
from media_archive.state.history import load_source_id, record_observation
from media_archive.state.initialization import load_initialization
from media_archive.state.registry import initialize_database
from tests.config_fixtures import registry_values, write_config


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = root / "settings.env"
        values = registry_values(root)
        values.update(ARCHIVE_DATA_TARGET_B_STATE="read_only", APP_REPORT_KEEP_RUNS="1")
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
