"""Verify the one-file transfer against real temporary files and the journal."""

from contextlib import contextmanager
import errno
from hashlib import sha256
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from media_archive._transfer_files import Part
from media_archive.audit import _Scan, _scan
from media_archive.config import load_config
from media_archive.devices import MountedVolume, _mount_id
from media_archive.sources import LocalSource, SourceEntry
from media_archive.state import StateError, load_registered_devices, open_database, transaction
from media_archive.state import history, journal
from media_archive.state.locking import Resource, collection_resource, lock_resources
from media_archive.state.registry import initialize_database
from media_archive.transfer import TransferConflict, execute_transfer
from tests.config_fixtures import registry_values, write_config


@contextmanager
def mounted(root: Path):
    fd = os.open(root, os.O_PATH | os.O_DIRECTORY)
    try:
        yield MountedVolume(root, _mount_id(fd), "synthetic", "8:1", "8:0", (), False, fd)
    finally:
        os.close(fd)


@contextmanager
def local_input(path: Path):
    with mounted(path.parent) as volume:
        yield LocalSource(volume, path.parent), Path(path.name)


def crash_worker(settings: str, operation: str, filename: str, phase: str) -> None:
    config = load_config(settings)
    publish = Part.publish

    def crash(part, *args):
        if phase == "after":
            publish(part, *args)
        os._exit(74)

    with (patch("media_archive.transfer.verify_volume",
                side_effect=lambda *args, **kwargs: mounted(config.local.volumes[0].root)),
          patch.object(Part, "publish", new=crash)):
        execute_transfer(config, int(operation), open_source=lambda: local_input(Path(filename)),
                         record_result=lambda *_: None)


class TransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = self.root / "settings.env"
        write_config(self.settings, registry_values(self.root))
        self.config = load_config(self.settings)
        self.database = self.config.app.database_file
        self.database.parent.mkdir(parents=True)
        initialize_database(self.config)
        self.volume = self.config.local.volumes[0].root
        self.volume.mkdir(parents=True)
        self.source = self.root / "source : file.jpg"
        self.data = b"synthetic media sample"
        self.source.write_bytes(self.data)
        self.destination = self.config.local.staging_root / "nested" / "copy.jpg"
        phone = next(device for device in load_registered_devices(self.database) if device.serial)
        with open_database(self.database) as connection:
            source_id = history.load_source_id(connection, "phone", "main")
        resources = [Resource("source", str(source_id))]
        with (lock_resources(self.database, resources) as locks, open_database(self.database, writable=True) as con,
              transaction(con)):
            content = history.record_content(con, sha256(self.data).hexdigest(), len(self.data))
            self.source_id = history.record_occurrence(con, "source", "/DCIM/example", content_id=content,
                                                       device_id=phone.device_id)
            self.target = history.record_occurrence(con, "staging", self.destination.as_posix(), content_id=content)
            self.operation = journal.plan_operation(
                con, locks, self.config, operation_key="synthetic-transfer", intent="Import", target_occurrence_id=self.target,
                required_resources=resources,
            )
        self.enterContext(patch("media_archive.transfer.verify_volume", side_effect=self._verify))

    def _verify(self, config, key, *, writable):
        self.assertTrue(writable)
        self.assertEqual(key.role, "local")
        return mounted(self.volume)

    def _execute(self, opener=None):
        return execute_transfer(self.config, self.operation,
                                open_source=opener if opener is not None else lambda: local_input(self.source),
                                record_result=lambda con, proof: history.record_import(con, self.source_id, proof))

    def _states(self) -> list[str]:
        with open_database(self.database) as con:
            return [row.state for row in journal.load_attempts(con, self.operation)]

    def _child_crash(self, phase: str) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "from tests.test_transfer import crash_worker; "
             "import sys; crash_worker(*sys.argv[1:])",
             str(self.settings), str(self.operation), str(self.source), phase],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 74, result.stderr)

    def test_rejects_file_targets_that_block_reserved_collection_directories(self) -> None:
        local = self.config.collection("photos_main").local_root.parent
        archive = next(device for device in load_registered_devices(self.database)
                       if device.key.role == "archive" and device.key.group_id == "data")
        cases = (
            ("local", local, "photos", None, collection_resource(self.config, "photos")),
            ("archive", Path(self.config.archive_mapping("photos_main", "data")).parent,
             None, archive.volume_id, Resource("archive", "data")),
        )
        for kind, path, collection_id, volume_id, resource in cases:
            with self.subTest(kind=kind):
                with (lock_resources(self.database, [resource]) as locks,
                      open_database(self.database, writable=True) as con, transaction(con)):
                    content = history.record_content(con, sha256(self.data).hexdigest(), len(self.data))
                    target = history.record_occurrence(con, kind, path.as_posix(), content_id=content,
                                                       collection_id=collection_id, volume_id=volume_id)
                    operation = journal.plan_operation(
                        con, locks, self.config, operation_key=f"reserved-{kind}", intent="Test",
                        target_occurrence_id=target, required_resources=[resource],
                    )
                with self.assertRaisesRegex(StateError, "blokowałby zarezerwowany katalog"):
                    execute_transfer(self.config, operation,
                                     open_source=lambda: self.fail("opened source for a reserved path"),
                                     record_result=lambda *_: self.fail("recorded a forbidden transfer"))
                with open_database(self.database) as con:
                    self.assertEqual(journal.load_attempts(con, operation), ())
        self.assertFalse(local.exists())

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_retry_syncs_ancestor_of_directory_left_by_failed_sync(self) -> None:
        sync = os.fsync
        staging = self.config.local.staging_root

        def fail_after_mkdir(fd):
            if Path(os.readlink(f"/proc/self/fd/{fd}")) == staging and self.destination.parent.is_dir():
                raise OSError(errno.EIO, "synthetic directory sync failure")
            sync(fd)

        with patch("media_archive._transfer_files.os.fsync", side_effect=fail_after_mkdir):
            with self.assertRaises(StateError):
                self._execute()
        self.assertTrue(self.destination.parent.is_dir())
        self.assertFalse(self.destination.exists())
        self.assertEqual(self._states(), ["failed"])

        synced = []

        def track_sync(fd):
            synced.append(Path(os.readlink(f"/proc/self/fd/{fd}")))
            sync(fd)

        with patch("media_archive._transfer_files.os.fsync", side_effect=track_sync):
            self._execute()
        self.assertEqual(synced[-3:], [self.destination.parent, staging, self.volume])
        self.assertEqual(self._states(), ["failed", "committed"])

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_rsync_publishes_verified_copy_and_keeps_source_mtime(self) -> None:
        original = self.source.stat().st_mtime_ns
        result = self._execute()
        self.assertEqual(self.destination.read_bytes(), self.data)
        self.assertEqual(self.destination.stat().st_mtime_ns, original)
        self.assertEqual(self.source.read_bytes(), self.data)
        self.assertEqual(self._states(), ["committed"])
        with open_database(self.database) as con:
            self.assertEqual(history.load_verifications(con, self.target)[0].id, result.verification_id)
        self.assertEqual(self._execute(lambda: self.fail("committed transfer reopened source")), result)

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_publication_without_rename_flags_uses_atomic_link(self) -> None:
        with patch("media_archive._transfer_files._rename_noreplace",
                   side_effect=OSError(errno.EOPNOTSUPP, "synthetic filesystem")):
            self._execute()
        self.assertEqual(self.destination.read_bytes(), self.data)
        self.assertEqual(self.destination.stat().st_nlink, 1)
        self.assertEqual(self._states(), ["committed"])

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_independent_read_rejects_a_corrupted_part(self) -> None:
        copy = Part.rsync_from

        def corrupt(part, source_fd):
            copy(part, source_fd)
            (Path(f"/proc/self/fd/{part.directory}") / "copy").write_bytes(b"x" * len(self.data))

        with patch.object(Part, "rsync_from", new=corrupt), self.assertRaisesRegex(StateError, "Niezależny odczyt"):
            self._execute()
        self.assertFalse(self.destination.exists())
        self.assertEqual(self._states(), ["failed"])
        self.assertEqual(len(list(self.destination.parent.glob(".media-archive-transfer-*/copy"))), 1)

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_crash_after_publication_reconciles_without_source_or_double_history(self) -> None:
        self._child_crash("after")
        self.assertEqual(self.destination.read_bytes(), self.data)
        self.assertEqual(self._states(), ["verified"])
        self._execute(lambda: self.fail("reconciliation reopened source"))
        self.assertEqual(self._states(), ["interrupted", "committed"])
        with open_database(self.database) as con:
            self.assertEqual(con.exec_driver_sql("SELECT count(*) FROM main.history_imports").scalar_one(), 1)
            self.assertEqual(len(history.load_verifications(con, self.target)), 2)

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_crash_before_publication_restarts_and_retains_old_part(self) -> None:
        self._child_crash("before")
        self.assertFalse(self.destination.exists())
        part, = self.destination.parent.glob(".media-archive-transfer-*/copy")
        self._execute()
        self.assertEqual(part.read_bytes(), self.data)
        self.assertEqual(self.destination.read_bytes(), self.data)
        self.assertEqual(self._states(), ["interrupted", "committed"])
        with open_database(self.database) as con:
            self.assertTrue(any(not entry.present for entry in history.load_observations(con, self.target)))

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_competing_destination_is_never_overwritten(self) -> None:
        publish = Part.publish

        def compete(part, *args):
            self.destination.write_bytes(b"unrelated")
            return publish(part, *args)

        with patch.object(Part, "publish", new=compete), self.assertRaises(TransferConflict):
            self._execute()
        with self.assertRaises(TransferConflict):
            self._execute(lambda: self.fail("conflict reopened source"))
        self.assertEqual(self.destination.read_bytes(), b"unrelated")
        self.assertEqual(self._states(), ["needs_review", "needs_review"])

    def test_interrupted_stream_keeps_partial_aside_and_retry_starts_from_zero(self) -> None:
        source = self.source

        class Stream:
            def __init__(self, interrupted):
                self.interrupted = interrupted

            def attributes(self, path):
                return SourceEntry.from_stat(path, source.stat())

            @contextmanager
            def read_chunks(self, path, *, expected):
                def chunks():
                    with source.open("rb") as data:
                        yield data.read(4)
                        if self.interrupted:
                            raise StateError("Testowy komunikat odłączenia ADB.")
                        yield data.read()
                yield chunks()

        @contextmanager
        def opener(interrupted):
            yield Stream(interrupted), Path("file.jpg")

        with self.assertRaisesRegex(StateError, "odłączenia"):
            self._execute(lambda: opener(True))
        self.assertFalse(self.destination.exists())
        part, = self.destination.parent.glob(".media-archive-transfer-*/copy")
        self.assertEqual(part.read_bytes(), self.data[:4])
        with mounted(self.volume) as volume:
            scan = _Scan()
            _scan(LocalSource(volume, self.destination.parent), scan)
        self.assertEqual(scan.entries, {})
        self._execute(lambda: opener(False))
        self.assertEqual(self.destination.read_bytes(), self.data)
        self.assertEqual(part.read_bytes(), self.data[:4])
        self.assertEqual(self._states(), ["failed", "committed"])
