"""Media history and initialization on temporary SQLite databases."""

from datetime import datetime, timezone
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy.engine import Connection

from media_archive.config import Configuration, load_config
from media_archive.state import (
    DeviceChange, DeviceKey, RegisteredDevice, StateError, create_database, load_registered_devices,
    load_registered_identities, migrate_database, open_database, register_configuration,
    transaction, validate_registered_configuration,
)
from media_archive.state import history, initialization, migrations
from tests.config_fixtures import registry_values, write_config


NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)
HASH = "a" * 64
OTHER_HASH = "b" * 64
ARCHIVE_PATH = "Data/Photos/All/Main/example.jpg"


class StateHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = self.root / "settings.env"
        self.path = self.root / "state" / "state.sqlite3"
        self.path.parent.mkdir()
        self.values = registry_values(self.root)
        self.values["APP_STATE_DIR"] = str(self.path.parent)
        for alias in ("C", "D"):
            self.values[f"ARCHIVE_DATA_TARGET_{alias}_UUID"] = f"synthetic-{alias}"
            self.values[f"ARCHIVE_DATA_TARGET_{alias}_MOUNT"] = str(self.root / f"archive-{alias}")
        self.values["ARCHIVE_DATA_TARGETS"] = "a,b,c,d"

    def config(self, **updates: str) -> Configuration:
        write_config(self.settings, self.values | updates)
        return load_config(self.settings)

    def prepare(self) -> None:
        create_database(self.path)
        register_configuration(self.config())
        self.devices = {device.key: device for device in load_registered_devices(self.path)}

    def phone(self) -> RegisteredDevice:
        return self.devices[DeviceKey("phone", "main")]

    def proof(self, connection: Connection, occurrence_id: int, run_id: str) -> int:
        return history.record_verification(
            connection, occurrence_id, run_id, verified_at=NOW, scope="full", read_sha256=HASH, bytes_read=42,
        )

    def test_current_migration_preserves_registration(self) -> None:
        old = self.root / "previous-revisions"
        versions = old / "versions"
        versions.mkdir(parents=True)
        packaged = migrations._SCRIPT_LOCATION
        shutil.copy(packaged / "env.py", old)
        for name in ("r0001_foundation.py", "r0002_registry.py"):
            shutil.copy(packaged / "versions" / name, versions)
        config = self.config()
        with patch.object(migrations, "_SCRIPT_LOCATION", old):
            create_database(self.path)
            register_configuration(config)
        keys = load_registered_identities(self.path)
        devices = load_registered_devices(self.path)
        self.assertEqual(migrate_database(self.path), "0008")
        self.assertEqual(load_registered_identities(self.path), keys)
        self.assertEqual(load_registered_devices(self.path), devices)
        validate_registered_configuration(config)
        with open_database(self.path) as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA main.foreign_key_check").all(), [])

    def test_same_content_distinct_paths_and_reused_path_keep_separate_evidence(self) -> None:
        self.prepare()
        with open_database(self.path, writable=True) as connection, transaction(connection):
            run = history.create_run(connection)
            original = history.record_content(connection, HASH, 42)
            self.assertEqual(history.record_content(connection, HASH.upper(), 42), original)
            other = history.record_content(connection, OTHER_HASH, 42)
            first = history.record_occurrence(connection, "staging", "/staging/first.jpg", content_id=original)
            second = history.record_occurrence(connection, "staging", "/staging/second.jpg", content_id=original)
            self.proof(connection, first, run)
            replacement = history.record_occurrence(connection, "staging", "/staging/first.jpg", content_id=other)
            unknown = history.record_occurrence(connection, "staging", "/staging/unread.jpg")
            history.record_observation(connection, unknown, run, observed_at=NOW, present=True, size=42)
        with open_database(self.path) as connection:
            self.assertNotEqual(first, second)
            self.assertEqual(history.load_occurrence(connection, second).content_id, original)
            self.assertEqual(history.load_occurrence(connection, replacement).path, "/staging/first.jpg")
            self.assertEqual([row.id for row in history.load_occurrences(connection, "staging", "/staging/first.jpg")],
                             [first, replacement])
            self.assertEqual(history.load_verifications(connection, replacement), ())
            self.assertEqual(history.load_verifications(connection, first)[0].result, "match")
            self.assertIsNone(history.load_occurrence(connection, unknown).content_id)
            self.assertTrue(history.load_observations(connection, unknown)[0].present)
            self.assertEqual(history.load_verifications(connection, unknown), ())

    def test_non_utf8_path_fails_safely_for_writes_and_lookups(self) -> None:
        self.prepare()
        path = "/staging/private-name-\udcff.jpg"
        with open_database(self.path, writable=True) as connection, transaction(connection):
            with self.assertRaisesRegex(StateError, "UTF-8") as failure:
                history.record_occurrence(connection, "staging", path)
            self.assertNotIn("private-name", str(failure.exception))
        with open_database(self.path) as connection:
            with self.assertRaisesRegex(StateError, "UTF-8") as failure:
                history.load_occurrences(connection, "staging", path)
            self.assertNotIn("private-name", str(failure.exception))

    def test_verified_import_survives_missing_staging_and_placement_is_separate(self) -> None:
        self.prepare()
        with open_database(self.path, writable=True) as connection, transaction(connection):
            run = history.create_run(connection)
            content = history.record_content(connection, HASH, 42)
            source = history.record_occurrence(
                connection, "source", "/DCIM/example.jpg", content_id=content, device_id=self.phone().device_id,
            )
            staging = history.record_occurrence(connection, "staging", "/staging/example.jpg", content_id=content)
            partial = history.record_verification(
                connection, staging, run, verified_at=NOW, scope="partial", read_sha256=HASH, bytes_read=42,
            )
            wrong_size = history.record_verification(
                connection, staging, run, verified_at=NOW, scope="full", read_sha256=HASH, bytes_read=41,
            )
            for verification in (partial, wrong_size):
                with self.assertRaises(StateError):
                    history.record_import(connection, source, verification)
            match = self.proof(connection, staging, run)
            imported = history.record_import(connection, source, match)
            self.assertEqual(history.record_import(connection, source, match), imported)
            history.record_observation(connection, staging, run, observed_at=NOW, present=False)

        with open_database(self.path) as connection:
            source_profile = history.load_occurrence(connection, source).source_id
            self.assertEqual(history.load_imports(connection, source_profile)[0].id, imported)
            self.assertFalse(history.load_observations(connection, staging)[-1].present)
            self.assertIsNone(history.load_placement(connection, imported))
        with open_database(self.path, writable=True) as connection, transaction(connection):
            local = history.record_occurrence(
                connection, "local", "/photos/example.jpg", content_id=content, collection_id="photos_main",
            )
            history.record_placement(connection, imported, self.proof(connection, local, run))
        with open_database(self.path) as connection:
            self.assertEqual(history.load_placement(connection, imported).local_occurrence_id, local)

    def test_archive_pair_survives_partial_copy_and_assignment_to_new_pair(self) -> None:
        self.prepare()
        a, b, c, d = [self.devices[DeviceKey("archive", alias, "data")] for alias in ("a", "b", "c", "d")]
        with open_database(self.path, writable=True) as connection, transaction(connection):
            run = history.create_run(connection)
            content = history.record_content(connection, HASH, 42)
            first = history.record_occurrence(
                connection, "local", "/photos/example.jpg", content_id=content, collection_id="photos_main",
            )
            second = history.record_occurrence(
                connection, "local", "/photos/later.jpg", content_id=content, collection_id="photos_main",
            )
            with self.assertRaises(StateError):
                history.assign_archive_pair(connection, first, a.device_id, a.device_id, ARCHIVE_PATH)
            with self.assertRaises(StateError):
                history.assign_archive_pair(connection, first, a.device_id, b.device_id, "Wrong/example.jpg")
            history.assign_archive_pair(connection, first, b.device_id, a.device_id, ARCHIVE_PATH)
            for device, path in ((c, ARCHIVE_PATH), (a, ARCHIVE_PATH + ".wrong")):
                wrong_copy = history.record_occurrence(connection, "archive", path, content_id=content,
                                                       volume_id=device.volume_id)
                with self.assertRaises(StateError):
                    history.record_archive_copy(connection, first, self.proof(connection, wrong_copy, run))
            archive = history.record_occurrence(
                connection, "archive", ARCHIVE_PATH, content_id=content, volume_id=a.volume_id,
            )
            history.record_archive_copy(connection, first, self.proof(connection, archive, run))
            history.assign_archive_pair(connection, second, c.device_id, d.device_id,
                                        "Data/Photos/All/Main/later.jpg")
            with self.assertRaises(StateError):
                history.assign_archive_pair(connection, first, c.device_id, d.device_id, ARCHIVE_PATH)
        with open_database(self.path) as connection:
            pair = history.load_archive_pair(connection, first)
            copies = history.load_archive_copies(connection, first)
            self.assertEqual({pair.first_device_id, pair.second_device_id}, {a.device_id, b.device_id})
            self.assertEqual([(copy.device_id, copy.volume_id) for copy in copies], [(a.device_id, a.volume_id)])
            self.assertEqual(history.load_archive_copies(connection, second), ())

    def test_archive_pair_requires_the_deepest_registered_collection(self) -> None:
        self.prepare()
        a = self.devices[DeviceKey("archive", "a", "data")].device_id
        b = self.devices[DeviceKey("archive", "b", "data")].device_id
        destination = "Data/Photos/1.to-review/example.jpg"
        with open_database(self.path, writable=True) as connection, transaction(connection):
            content = history.record_content(connection, HASH, 42)
            parent = history.record_occurrence(
                connection, "local", "/photos/example.jpg", content_id=content, collection_id="photos",
            )
            child = history.record_occurrence(
                connection, "local", "/photos/1.to-review/example.jpg", content_id=content, collection_id="review",
            )
            with self.assertRaisesRegex(StateError, "innego zarejestrowanego zbioru"):
                history.assign_archive_pair(connection, parent, a, b, destination)
            self.assertIsNone(history.load_archive_pair(connection, parent))
            history.assign_archive_pair(connection, child, a, b, destination)
            self.assertEqual(history.load_archive_pair(connection, child).required_path, destination)
            history.assign_archive_pair(connection, parent, a, b, "Data/Photos/1.to-review-old/example.jpg")

    def test_archive_file_cannot_block_a_reserved_directory(self) -> None:
        self.prepare()
        a = self.devices[DeviceKey("archive", "a", "data")].device_id
        b = self.devices[DeviceKey("archive", "b", "data")].device_id
        with open_database(self.path, writable=True) as connection, transaction(connection):
            content = history.record_content(connection, HASH, 42)
            parent = history.record_occurrence(
                connection, "local", "/photos/README", content_id=content, collection_id="photos",
            )
            for destination in ("Data/Photos/All", "Data/Photos/All/Main"):
                with self.subTest(destination=destination), self.assertRaisesRegex(StateError, "pozostać katalogiem"):
                    history.assign_archive_pair(connection, parent, a, b, destination)
            self.assertIsNone(history.load_archive_pair(connection, parent))
            history.assign_archive_pair(connection, parent, a, b, "Data/Photos/All-old/README")
            child = history.record_occurrence(
                connection, "local", "/photos/All/Main/README", content_id=content, collection_id="photos_main",
            )
            history.assign_archive_pair(connection, child, a, b, "Data/Photos/All/Main/README")

    def test_archive_file_cannot_block_a_directory_reserved_by_removed_collection(self) -> None:
        self.values["COLLECTION_REVIEW_ARCHIVE_RELATIVE"] = "Photos/Queue/Review"
        self.values["COLLECTION_REVIEW_LOCAL_ROOT"] = self.values["COLLECTION_PHOTOS_LOCAL_ROOT"] + "/Queue/Review"
        self.prepare()
        self.values = {key: value for key, value in self.values.items() if not key.startswith("COLLECTION_REVIEW_")}
        removed = self.config(COLLECTIONS=self.values["COLLECTIONS"].replace(",review,", ","))
        self.assertNotIn("review", [collection.id for collection in removed.collections])
        register_configuration(removed)
        a = self.devices[DeviceKey("archive", "a", "data")].device_id
        b = self.devices[DeviceKey("archive", "b", "data")].device_id
        with open_database(self.path, writable=True) as connection, transaction(connection):
            content = history.record_content(connection, HASH, 42)
            parent = history.record_occurrence(
                connection, "local", "/photos/Queue", content_id=content, collection_id="photos",
            )
            with self.assertRaisesRegex(StateError, "pozostać katalogiem"):
                history.assign_archive_pair(connection, parent, a, b, "Data/Photos/Queue")
            self.assertIsNone(history.load_archive_pair(connection, parent))

    def test_initialization_rejects_non_utf8_checkpoint_data(self) -> None:
        self.prepare()
        invalid = "private-name-\udcff"
        with open_database(self.path, writable=True) as connection, transaction(connection):
            source = history.load_source_id(connection, "phone", "main")
            run = history.create_run(connection)
            for boundary, scopes in ((invalid, ("local",)), ("baseline", (invalid,))):
                with self.assertRaisesRegex(StateError, "UTF-8") as failure:
                    initialization.start_initialization(
                        connection, source, run, policy="import_missing", boundary=boundary,
                        comparison_scopes=scopes, processing_scopes=("import",),
                    )
                self.assertIsNone(failure.exception.__context__)
                self.assertEqual(initialization.load_initialization(connection, source).status, "pending")
            initialization.start_initialization(
                connection, source, run, policy="import_missing", boundary="zdjęcia",
                comparison_scopes=("local",), processing_scopes=("import",),
            )
            with self.assertRaisesRegex(StateError, "UTF-8") as failure:
                initialization.record_initialization_progress(
                    connection, source, run, phase="comparison", scope="local", cursor=invalid,
                )
            self.assertIsNone(failure.exception.__context__)
            self.assertIsNone(initialization.load_initialization(connection, source).scopes[0].cursor)

    def test_initialization_resumes_and_completion_survives_device_replacement(self) -> None:
        self.prepare()
        phone = self.phone()
        with open_database(self.path, writable=True) as connection, transaction(connection):
            source = history.load_source_id(connection, "phone", "main")
            run = history.create_run(connection)
            self.assertEqual(initialization.load_initialization(connection, source).status, "pending")
            initialization.start_initialization(
                connection, source, run, policy="import_missing", boundary="first-scan",
                comparison_scopes=("local", "archive"), processing_scopes=("fill-gaps",),
            )
            item = history.record_occurrence(connection, "source", "/DCIM/example.jpg", device_id=phone.device_id)
            self.assertEqual(history.load_occurrence(connection, item).source_id, source)
            initialization.record_initialization_progress(
                connection, source, run, phase="comparison", scope="local", cursor="file-42",
            )
            with self.assertRaises(StateError):
                initialization.finish_initialization(connection, source, run)
            with self.assertRaises(StateError):
                initialization.record_initialization_progress(
                    connection, source, run, phase="processing", scope="fill-gaps", completed=True,
                )

        with open_database(self.path, writable=True) as connection, transaction(connection):
            state = initialization.load_initialization(connection, source)
            self.assertEqual((state.status, state.policy, state.boundary), ("in_progress", "import_missing", "first-scan"))
            self.assertEqual(next(scope.cursor for scope in state.scopes if scope.scope == "local"), "file-42")
            resumed = history.create_run(connection)
            for scope in ("local", "archive"):
                initialization.record_initialization_progress(
                    connection, source, resumed, phase="comparison", scope=scope, completed=True,
                )
            with self.assertRaises(StateError):
                initialization.finish_initialization(connection, source, resumed)
            initialization.record_initialization_progress(
                connection, source, resumed, phase="processing", scope="fill-gaps", completed=True,
            )
            initialization.finish_initialization(connection, source, resumed)

        changed = self.config(PHONE_MAIN_SERIAL="new-phone", PHONE_MAIN_LABEL="Travel", PHONE_MAIN_INITIAL_POLICY="baseline_skip_missing")
        register_configuration(changed, device_changes=[DeviceChange(phone, "replace")])
        with open_database(self.path) as connection:
            state = initialization.load_initialization(connection, source)
            self.assertEqual((state.status, state.policy, state.completed_run_id), ("completed", "import_missing", resumed))
            self.assertEqual(history.load_occurrence(connection, item).device_id, phone.device_id)

    def test_baseline_skip_requires_complete_comparison_and_same_phone(self) -> None:
        self.prepare()
        phone = self.phone()
        with open_database(self.path, writable=True) as connection, transaction(connection):
            run = history.create_run(connection)
            content = history.record_content(connection, HASH, 42)
            source = history.record_occurrence(
                connection, "source", "/DCIM/example.jpg", content_id=content, device_id=phone.device_id,
            )
            profile = history.load_occurrence(connection, source).source_id
            initialization.start_initialization(
                connection, profile, run, policy="baseline_skip_missing", boundary="snapshot",
                comparison_scopes=("compare",), processing_scopes=("skip",), baseline_device_id=phone.device_id,
            )
            with self.assertRaises(StateError):
                initialization.record_baseline_skip(connection, source, run)
            initialization.record_initialization_progress(
                connection, profile, run, phase="comparison", scope="compare", completed=True,
            )

        register_configuration(self.config(PHONE_MAIN_SERIAL="new-phone"), device_changes=[DeviceChange(phone, "replace")])
        replacement = next(device for device in load_registered_devices(self.path) if device.key == phone.key)
        with open_database(self.path, writable=True) as connection, transaction(connection):
            new_occurrence = history.record_occurrence(
                connection, "source", "/DCIM/example.jpg", content_id=content, device_id=replacement.device_id,
            )
            with self.assertRaises(StateError):
                initialization.record_baseline_skip(connection, new_occurrence, run)
            initialization.record_baseline_skip(connection, source, run)
            self.assertEqual(history.load_imports(connection, profile), ())
            self.assertEqual(initialization.load_baseline_skips(connection, profile)[0].occurrence_id, source)

    def test_batch_rollback_and_write_guard(self) -> None:
        self.prepare()
        with open_database(self.path, writable=True) as connection:
            with self.assertRaisesRegex(StateError, "transakcji"):
                history.create_run(connection)
            with connection.begin(), self.assertRaisesRegex(StateError, "transakcji"):
                history.create_run(connection)
            with self.assertRaisesRegex(RuntimeError, "interrupt"):
                with transaction(connection):
                    content = history.record_content(connection, HASH, 42)
                    occurrence = history.record_occurrence(connection, "staging", "/stage/example", content_id=content)
                    raise RuntimeError("interrupt")
        with open_database(self.path) as connection:
            with self.assertRaises(StateError):
                history.load_occurrence(connection, occurrence)
            with self.assertRaises(StateError):
                history.create_run(connection)


if __name__ == "__main__":
    unittest.main()
