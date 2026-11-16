"""Journal integration and process-held resource locks on temporary state."""

from datetime import datetime, timezone
from pathlib import Path
from select import select
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy.engine import Connection

from media_archive.state import (
    StateError, create_database, load_registered_devices, migrate_database, open_database,
    register_configuration, transaction,
)
from media_archive.state import history, journal, migrations
from media_archive.state.locking import OperationBusy, OperationLocks, Resource, collection_resource, lock_resources
from media_archive.config import load_config
from tests.config_fixtures import registry_values, write_config


WORKER = """
import sys
from media_archive.state import open_database, transaction
from media_archive.state import history, journal
from media_archive.state.locking import OperationBusy, Resource, lock_resources

path, kind, key, *args = sys.argv[1:]
try:
    with lock_resources(path, [Resource(kind, key)]) as locks:
        if args:
            with open_database(path, writable=True) as connection, transaction(connection):
                run = history.create_run(connection)
                attempt = journal.start_attempt(connection, locks, int(args[0]), run)
                journal.advance_attempt(connection, locks, attempt, 'transferring')
        print('ready', flush=True)
        sys.stdin.read()
except OperationBusy:
    sys.exit(23)
"""

HASH = "a" * 64
NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


class StateJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.root / "state" / "state.sqlite3"
        self.path.parent.mkdir()
        config_file = self.root / "settings.env"
        values = registry_values(self.root)
        values["APP_STATE_DIR"] = str(self.path.parent)
        write_config(config_file, values)
        self.config = load_config(config_file)
        self.photos = collection_resource(self.config, "photos")
        self.documents = collection_resource(self.config, "documents")

    def prepare(self) -> int:
        create_database(self.path)
        register_configuration(self.config)
        phone = next(device for device in load_registered_devices(self.path) if device.serial)
        with lock_resources(self.path, [self.photos]) as locks:
            with open_database(self.path, writable=True) as connection, transaction(connection):
                content = history.record_content(connection, HASH, 42)
                self.source = history.record_occurrence(
                    connection, "source", "/DCIM/example.jpg", content_id=content, device_id=phone.device_id,
                )
                self.target = history.record_occurrence(
                    connection, "staging", "/stage/example.jpg", content_id=content,
                )
                return journal.plan_operation(
                    connection, locks, self.config, operation_key="copy-example", intent="Import one item",
                    target_occurrence_id=self.target, required_resources=[self.photos],
                )

    def proof(self, connection: Connection, run: str, target: int) -> int:
        return history.record_verification(
            connection, target, run, verified_at=NOW, scope="full", read_sha256=HASH, bytes_read=42,
        )

    def test_migration_preserves_existing_run_and_evidence(self) -> None:
        old = self.root / "previous-revisions"
        versions = old / "versions"
        versions.mkdir(parents=True)
        shutil.copy(migrations._SCRIPT_LOCATION / "env.py", old)
        for revision in migrations._SCRIPT_LOCATION.glob("versions/r000[1-3]_*.py"):
            shutil.copy(revision, versions)
        with patch.object(migrations, "_SCRIPT_LOCATION", old):
            create_database(self.path)
            with open_database(self.path, writable=True) as connection, transaction(connection):
                run = history.create_run(connection)
                content = history.record_content(connection, HASH, 42)
                occurrence = history.record_occurrence(connection, "staging", "/stage/example", content_id=content)
                proof = self.proof(connection, run, occurrence)
        self.assertEqual(migrate_database(self.path), "0007")
        with open_database(self.path) as connection:
            self.assertEqual(history.load_verifications(connection, occurrence)[0].id, proof)
            self.assertEqual(journal.load_unfinished(connection), ())

    def test_conflicting_process_cannot_lock_until_owner_exits(self) -> None:
        operation = self.prepare()
        command = [sys.executable, "-I", "-c", WORKER, str(self.path)]
        with subprocess.Popen(
            [*command, "collection", self.photos.id, str(operation)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as owner:
            try:
                self.assertTrue(select([owner.stdout], [], [], 10)[0], "Worker did not acquire locks")
                self.assertEqual(owner.stdout.readline().strip(), "ready")
                for key, expected in ((self.photos.id, 23), (self.documents.id, 0)):
                    result = subprocess.run([*command, "collection", key], input="", capture_output=True,
                                            timeout=10, text=True)
                    self.assertEqual(result.returncode, expected, result.stderr)
            finally:
                owner.kill()
                owner.wait(timeout=10)

        with lock_resources(self.path, [self.photos]) as locks:
            with open_database(self.path, writable=True) as connection, transaction(connection):
                previous = journal.load_attempts(connection, operation)[0]
                run = history.create_run(connection)
                with self.assertRaises(StateError):
                    journal.start_attempt(connection, locks, operation, run)
                restarted = journal.start_attempt(connection, locks, operation, run, recovery="restart")
                self.assertEqual([row.state for row in journal.load_attempts(connection, operation)],
                                 ["interrupted", "planned"])
                with self.assertRaises(StateError):
                    journal.advance_attempt(connection, locks, previous.id, "transferred")
                journal.stop_attempt(connection, locks, restarted, "failed", error_code="source_unavailable")
                self.assertEqual(journal.load_events(connection, restarted)[-1].error_code, "source_unavailable")

    def test_lock_group_releases_partial_acquisition_and_normalizes_children(self) -> None:
        create_database(self.path)
        documents = self.documents
        self.assertEqual(collection_resource(self.config, "photos_main"), self.photos)
        with lock_resources(self.path, [self.photos]):
            with self.assertRaises(OperationBusy), lock_resources(self.path, [documents, self.photos]):
                self.fail("Conflicting lock group was acquired")
            with lock_resources(self.path, [documents]):
                pass

    def test_collection_lock_survives_reassignment_and_checks_overlapping_roots(self) -> None:
        create_database(self.path)
        other_id = self.config.collection("documents").model_copy(update={"local_root": Path(self.photos.id)})
        changed = self.config.model_copy(update={"collections": (other_id,)})
        self.assertEqual(collection_resource(changed, "documents"), self.photos)
        child_root = Resource("collection", str(Path(self.photos.id) / "nested"))
        for held, colliding in ((self.photos, child_root), (child_root, self.photos)):
            with lock_resources(self.path, [held]):
                with self.assertRaises(OperationBusy), lock_resources(self.path, [colliding]):
                    self.fail("Overlapping root acquired")
                with lock_resources(self.path, [self.documents]):
                    pass

    def test_fabricated_and_released_lock_handles_cannot_take_over(self) -> None:
        operation = self.prepare()
        with lock_resources(self.path, [self.photos]) as released:
            pass
        with lock_resources(self.path, [self.photos]) as owner:
            with open_database(self.path, writable=True) as connection, transaction(connection):
                attempt = journal.start_attempt(connection, owner, operation, history.create_run(connection))
                fake = OperationLocks(self.path, frozenset([self.photos]))
                run = history.create_run(connection)
                for invalid in (fake, released):
                    with self.assertRaisesRegex(StateError, "aktywnych blokad"):
                        journal.start_attempt(connection, invalid, operation, run, recovery="reconcile")
                self.assertEqual([(row.id, row.state) for row in journal.load_attempts(connection, operation)],
                                 [(attempt, "planned")])

    def test_archive_plan_requires_registered_group_lock(self) -> None:
        self.prepare()
        archive = next(device for device in load_registered_devices(self.path) if device.key.group_id == "data")
        group = Resource("archive", "data")
        with lock_resources(self.path, [self.photos, group]) as locks:
            with open_database(self.path, writable=True) as connection, transaction(connection):
                content = history.record_content(connection, HASH, 42)
                occurrence = history.record_occurrence(
                    connection, "archive", "Data/Photos/All/Main/example.jpg", content_id=content,
                    volume_id=archive.volume_id,
                )
                kwargs = dict(operation_key="archive-example", intent="Backup example", target_occurrence_id=occurrence)
                with self.assertRaisesRegex(StateError, "grupy archiwalnej"):
                    journal.plan_operation(connection, locks, self.config, required_resources=[self.photos], **kwargs)
                operation = journal.plan_operation(connection, locks, self.config, required_resources=[self.photos, group], **kwargs)
                self.assertEqual(journal.load_resources(connection, operation), {self.photos, group})
        with lock_resources(self.path, [group]) as incomplete:
            with open_database(self.path, writable=True) as connection, transaction(connection):
                with self.assertRaisesRegex(StateError, "wszystkich zapisanych zasobów"):
                    journal.start_attempt(connection, incomplete, operation, history.create_run(connection))

    def test_publication_recovery_requires_fresh_evidence_and_commits_once(self) -> None:
        operation = self.prepare()
        with lock_resources(self.path, [self.photos]) as locks:
            with open_database(self.path, writable=True) as connection, transaction(connection):
                run = history.create_run(connection)
                attempt = journal.start_attempt(connection, locks, operation, run)
                with self.assertRaises(StateError):
                    journal.advance_attempt(connection, locks, attempt, "published")
                for state in ("transferring", "transferred", "verifying"):
                    journal.advance_attempt(connection, locks, attempt, state)
                old_proof = self.proof(connection, run, self.target)
                journal.advance_attempt(connection, locks, attempt, "verified", verification_id=old_proof)
                journal.advance_attempt(connection, locks, attempt, "published")
                with self.assertRaises(StateError):
                    journal.start_attempt(connection, locks, operation, history.create_run(connection),
                                          recovery="reconcile")

        def record_result(connection: Connection, verification_id: int) -> None:
            history.record_import(connection, self.source, verification_id)

        with lock_resources(self.path, [self.photos]) as locks:
            with open_database(self.path, writable=True) as connection, transaction(connection):
                journal.start_attempt(connection, locks, operation, history.create_run(connection),
                                      recovery="reconcile")

        with lock_resources(self.path, [self.photos]) as locks:
            with open_database(self.path, writable=True) as connection, transaction(connection):
                run = history.create_run(connection)
                with self.assertRaises(StateError):
                    journal.start_attempt(connection, locks, operation, run, recovery="restart")
                stale_same_run = self.proof(connection, run, self.target)
                resumed = journal.start_attempt(connection, locks, operation, run, recovery="reconcile")
                for stale in (old_proof, stale_same_run):
                    with self.assertRaises(StateError):
                        journal.advance_attempt(connection, locks, resumed, "verified", verification_id=stale)
                proof = self.proof(connection, run, self.target)
                journal.advance_attempt(connection, locks, resumed, "verified", verification_id=proof)
                journal.advance_attempt(connection, locks, resumed, "published")

                def interrupt(connection: Connection, verification_id: int) -> None:
                    record_result(connection, verification_id)
                    raise RuntimeError("interrupted result")

                with self.assertRaisesRegex(RuntimeError, "interrupted result"):
                    journal.commit_result(connection, locks, resumed, interrupt)
                source_id = history.load_occurrence(connection, self.source).source_id
                self.assertEqual(history.load_imports(connection, source_id), ())
                self.assertEqual(journal.load_attempts(connection, operation)[-1].state, "published")
                with self.assertRaises(StateError):
                    journal.commit_result(connection, locks, resumed, lambda *_: None)
                journal.commit_result(connection, locks, resumed, record_result)
                with self.assertRaises(StateError):
                    journal.commit_result(connection, locks, resumed, record_result)
                with self.assertRaises(StateError):
                    journal.start_attempt(connection, locks, operation, run)

        with open_database(self.path) as connection:
            self.assertEqual(len(history.load_imports(connection, source_id)), 1)
            self.assertEqual([row.state for row in journal.load_attempts(connection, operation)],
                             ["interrupted", "interrupted", "committed"])
            self.assertEqual(journal.load_unfinished(connection), ())

    def test_reconciliation_can_retry_transfer_after_fresh_absence(self) -> None:
        operation = self.prepare()
        with lock_resources(self.path, [self.photos]) as locks:
            with open_database(self.path, writable=True) as connection, transaction(connection):
                run = history.create_run(connection)
                attempt = journal.start_attempt(connection, locks, operation, run)
                for state in ("transferring", "transferred", "verifying"):
                    journal.advance_attempt(connection, locks, attempt, state)
                journal.advance_attempt(connection, locks, attempt, "verified",
                                        verification_id=self.proof(connection, run, self.target))
        with lock_resources(self.path, [self.photos]) as locks:
            with open_database(self.path, writable=True) as connection, transaction(connection):
                run = history.create_run(connection)
                stale = history.record_observation(connection, self.target, run, observed_at=NOW, present=False)
                attempt = journal.start_attempt(connection, locks, operation, run, recovery="reconcile")
                present = history.record_observation(connection, self.target, run, observed_at=NOW, present=True)
                for invalid in (stale, present):
                    with self.assertRaisesRegex(StateError, "braku pliku"):
                        journal.restart_after_missing_destination(connection, locks, attempt, invalid)
                absent = history.record_observation(connection, self.target, run, observed_at=NOW, present=False)
                journal.restart_after_missing_destination(connection, locks, attempt, absent)
                self.assertEqual(journal.load_events(connection, attempt)[-1].observation_id, absent)
                journal.advance_attempt(connection, locks, attempt, "transferring")
                self.assertEqual([row.state for row in journal.load_attempts(connection, operation)],
                                 ["interrupted", "transferring"])


if __name__ == "__main__":
    unittest.main()
