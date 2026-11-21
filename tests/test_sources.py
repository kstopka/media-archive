"""Exercise source roots and the actual shell arguments with a synthetic ADB client."""

from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import resource
import shutil
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from media_archive.cli import main
from media_archive.config import ConfigError, load_config
from media_archive.devices import MountedVolume, _mount_id
from media_archive.phone_import import import_phone
from media_archive.sources import ReadCancelled, SourceDigest, hash_source_file, open_adb_source, open_camera_source
from media_archive.state import (
    DeviceChange, DeviceKey, StateError, history, initialization, load_registered_devices,
    open_database, register_configuration, transaction,
)
from media_archive.state._installation import OperationBusy
from media_archive.state.registry import initialize_database
from tests.config_fixtures import registry_values, write_config


class SourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.android = self.root / "root with spaces"
        self.android.mkdir()
        self.settings = self.root / "settings.env"
        self.values = registry_values(self.root)
        self.values.update(PHONE_MAIN_SERIAL="test-phone", PHONE_MAIN_CAMERA_DIR=str(self.android))
        write_config(self.settings, self.values)
        self.config = load_config(self.settings)
        self.config.app.state_dir.mkdir(parents=True)
        initialize_database(self.config)

    def _fake_adb(self) -> Path:
        client = self.root / "adb"
        client.write_text(r'''#!/bin/sh
status=$(cat "$SOURCE_TEST_PHONE_STATE")
if [ "$1" = devices ]; then
    printf 'List of devices attached\nother-phone device transport_id:1\n'
    if [ "$status" != missing ]; then
        printf 'test-phone %s transport_id:2\n' "$status"
    fi
    exit 0
fi
[ "$1" = -s ] && [ "$2" = test-phone ] || exit 1
if [ "$3" = features ]; then
    printf 'cmd\nshell_v2\n'
    exit 0
fi
[ "$3" = shell ] && [ "$4" = -T ] && [ "$5" = -n ] || exit 1
case "$status" in
    disconnected) exit 1 ;;
    partial) printf 'partial'; exit 1 ;;
    short) printf 'short'; exit 0 ;;
    device) exec sh -c "$6" ;;
    *) exit 1 ;;
esac
''', encoding="utf-8")
        client.chmod(0o700)
        status_file = self.root / "phone-state"
        status_file.write_text("device", encoding="utf-8")
        env = {
            "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
            "SOURCE_TEST_PHONE_STATE": str(status_file),
        }
        self.enterContext(patch.dict(os.environ, env))
        return status_file

    def _import_baseline(self, policy: str, files: dict[str, bytes]) -> int:
        phone = next(device for device in load_registered_devices(self.config.app.database_file)
                     if device.key == DeviceKey("phone", "main"))
        with open_database(self.config.app.database_file, writable=True) as connection, transaction(connection):
            source_id = history.load_source_id(connection, "phone", "main")
            run = history.create_run(connection)
            initialization.start_initialization(
                connection, source_id, run, policy=policy,
                boundary=json.dumps({"root": str(self.android), "device_id": phone.device_id}),
                comparison_scopes=["copies"], processing_scopes=["baseline"],
                baseline_device_id=phone.device_id if policy == "baseline_skip_missing" else None,
            )
            decisions = []
            for name, data in files.items():
                content_id = history.record_content(connection, sha256(data).hexdigest(), len(data))
                occurrence = history.record_occurrence(connection, "source", str(self.android / name),
                                                       content_id=content_id, device_id=phone.device_id)
                history.record_observation(connection, occurrence, run, observed_at=datetime.now(timezone.utc),
                                           present=True, size=len(data))
                decisions.append({"occurrence_id": occurrence, "decision": "missing_local" if policy == "import_missing"
                                  else "baseline_skip", "path": str(self.android / name),
                                  "local_occurrence_ids": [], "archive_reads": []})
            initialization.record_initialization_progress(
                connection, source_id, run, phase="comparison", scope="copies", completed=True,
                cursor=json.dumps({"files": decisions, "local_run_id": run, "audit_id": run}),
            )
            if policy == "baseline_skip_missing":
                for item in decisions:
                    initialization.record_baseline_skip(connection, item["occurrence_id"], run)
                initialization.record_initialization_progress(connection, source_id, run, phase="processing",
                                                               scope="baseline", completed=True)
                initialization.finish_initialization(connection, source_id, run)
        return source_id

    def _import_staging(self) -> None:
        root = self.config.local.volumes[0].root
        root.mkdir(parents=True)
        fd = os.open(root, os.O_PATH | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        volume = MountedVolume(root, _mount_id(fd), "synthetic", "8:1", "8:0", (), False, fd)
        for module in ("import_planning", "transfer"):
            self.enterContext(patch(f"media_archive.{module}.verify_volume",
                                    side_effect=lambda *_args, **_kwargs: nullcontext(volume)))

    def test_phone_import_fills_only_baseline_until_complete_then_imports_new_media(self) -> None:
        self._fake_adb()
        self._import_staging()
        source_id = self._import_baseline("import_missing", {"a.jpg": b"first", "b.mp4": b"second"})
        (self.android / "a.jpg").write_bytes(b"first")
        (self.android / "duplicate.mp4").write_bytes(b"second")
        (self.android / "new.jpg").write_bytes(b"later")
        partial = import_phone(self.config, "main")
        self.assertEqual(partial.code, 3)
        self.assertEqual([item.path.name for item in partial.results], ["a.jpg"])
        with open_database(self.config.app.database_file) as connection:
            self.assertEqual(initialization.load_initialization(connection, source_id).status, "in_progress")
        (self.android / "b.mp4").write_bytes(b"second")
        finished = import_phone(self.config, "main")
        self.assertEqual(finished.code, 0)
        self.assertEqual([item.path.name for item in finished.results], ["b.mp4"])
        with open_database(self.config.app.database_file) as connection:
            self.assertEqual(initialization.load_initialization(connection, source_id).status, "completed")
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = main(["--config", str(self.settings), "import", "--source", "main"])
        self.assertEqual(code, 0, output.getvalue())
        with open_database(self.config.app.database_file) as connection:
            imported = history.load_imports(connection, source_id)
            self.assertEqual({Path(history.load_occurrence(connection, row.staging_occurrence_id).path).read_bytes()
                              for row in imported}, {b"first", b"second", b"later"})
            self.assertEqual(len(imported), 3)
        self.assertEqual((self.android / "b.mp4").read_bytes(), b"second")
        self.assertEqual(import_phone(self.config, "main").results, ())

    def test_phone_import_respects_baseline_skips_and_records_no_success_after_disconnect(self) -> None:
        status = self._fake_adb()
        self._import_staging()
        source_id = self._import_baseline("baseline_skip_missing", {"skip.jpg": b"skip"})
        (self.android / "skip.jpg").write_bytes(b"skip")
        (self.android / "new.jpg").write_bytes(b"new")
        for value, diagnostic in (("unauthorized", "autoryzacji"), ("offline", "gotowy"), ("missing", "odłączony")):
            status.write_text(value, encoding="utf-8")
            with self.assertRaisesRegex(StateError, diagnostic):
                import_phone(self.config, "main")
        status.write_text("device", encoding="utf-8")

        def disconnect(message: str) -> None:
            if message.startswith("Import z telefonu"):
                status.write_text("disconnected", encoding="utf-8")

        failed = import_phone(self.config, "main", announce=disconnect)
        self.assertEqual(failed.code, 3)
        self.assertEqual([item.status for item in failed.results], ["failed"])
        with open_database(self.config.app.database_file) as connection:
            self.assertEqual(history.load_imports(connection, source_id), ())
        status.write_text("device", encoding="utf-8")
        result = import_phone(self.config, "main")
        self.assertEqual(result.code, 0)
        self.assertEqual([item.path.name for item in result.results], ["new.jpg"])
        self.assertEqual((self.android / "skip.jpg").read_bytes(), b"skip")

    def test_camera_source_lists_attributes_and_reads_only_configured_tree(self) -> None:
        mount = self.root / "card"
        source_root = mount / "DCIM"
        source_root.mkdir(parents=True)
        (mount / "excluded.jpg").write_bytes(b"outside")
        (source_root / "image.jpg").write_bytes(b"inside")
        (source_root / "shortcut").symlink_to(mount / "excluded.jpg")
        descriptor = os.open(mount, os.O_PATH | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        volume = MountedVolume(mount, _mount_id(descriptor), "synthetic", "8:1", "8:0", (), True, descriptor)
        with patch("media_archive.sources.verify_volume", return_value=nullcontext(volume)) as verify:
            with open_camera_source(self.config, "hero8", "a", mount) as source:
                self.assertEqual([entry.path for entry in source.list_directory()], [Path("image.jpg"), Path("shortcut")])
                self.assertEqual(source.attributes(Path("image.jpg")).size, 6)
                self.assertTrue(stat.S_ISLNK(source.attributes(Path("shortcut")).mode))
                with source.read_chunks(Path("image.jpg")) as chunks:
                    self.assertEqual(b"".join(chunks), b"inside")
                self.assertEqual(hash_source_file(source, Path("image.jpg")), SourceDigest(sha256(b"inside").hexdigest(), 6))
                for invalid in ("../excluded.jpg", "shortcut"):
                    with self.subTest(path=invalid), self.assertRaises(StateError), source.read_chunks(Path(invalid)):
                        pass
            verify.assert_called_once_with(self.config, DeviceKey("card", "a"), mount)

    def test_local_hash_rejects_replacement_before_reading_bytes(self) -> None:
        mount = self.root / "card"
        scope = mount / "DCIM"
        scope.mkdir(parents=True)
        path = Path("image.jpg")
        original = scope / path
        original.write_bytes(b"original")
        replacement = scope / "replacement.jpg"
        replacement.write_bytes(b"replaced")
        descriptor = os.open(mount, os.O_PATH | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        volume = MountedVolume(mount, _mount_id(descriptor), "synthetic", "8:1", "8:0", (), True, descriptor)

        updates = []

        def replace_after_metadata(done: int, total: int) -> None:
            updates.append(done)
            if done == 0:
                replacement.replace(original)

        with patch("media_archive.sources.verify_volume", return_value=nullcontext(volume)):
            with open_camera_source(self.config, "hero8", "a", mount) as source:
                with self.assertRaisesRegex(StateError, "zmienił się"):
                    hash_source_file(source, path, progress=replace_after_metadata)
        self.assertEqual(updates, [0])

    def test_adb_handles_literal_shell_names_and_binary_bytes(self) -> None:
        self._fake_adb()
        name = "-żółć '$(touch SHOULD_NOT_EXIST)'\nphoto.jpg"
        folder = self.android / "nested dir"
        folder.mkdir()
        photo = folder / name
        byte_content = b"\x00\xff\nphoto\r\n"
        photo.write_bytes(byte_content)
        (self.android / "shortcut").symlink_to(self.root, target_is_directory=True)

        with open_adb_source(self.config, "main") as source:
            self.assertEqual(len(source.list_directory()), 2)
            entry, = source.list_directory(Path(folder.name))
            self.assertEqual(entry.path, Path(folder.name) / name)
            self.assertEqual(source.attributes(entry.path), entry)
            self.assertEqual(entry.size, len(byte_content))
            with source.read_chunks(entry.path) as chunks:
                self.assertEqual(b"".join(chunks), byte_content)
            with self.assertRaises(StateError):
                source.list_directory(Path("shortcut"))
            with self.assertRaises(StateError):
                source.attributes(Path("../settings.env"))
        self.assertFalse((folder / "SHOULD_NOT_EXIST").exists())
        self.assertFalse((self.android / "SHOULD_NOT_EXIST").exists())

    def test_adb_distinguishes_empty_directory_from_unavailable_source(self) -> None:
        status = self._fake_adb()
        for state, diagnostic in (("missing", "odłączony"), ("unauthorized", "autoryzacji"), ("offline", "gotowy")):
            status.write_text(state, encoding="utf-8")
            with self.subTest(state=state), self.assertRaisesRegex(StateError, diagnostic), open_adb_source(self.config, "main"):
                pass
        status.write_text("device", encoding="utf-8")
        with open_adb_source(self.config, "main") as source:
            self.assertEqual(source.list_directory(), ())
            # A bad UTF-8 filename must not be silently discarded.
            bad_name = os.fsencode(self.android) + b"/bad-\xff.jpg"
            descriptor = os.open(bad_name, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(descriptor)
            with self.assertRaisesRegex(StateError, "UTF-8"):
                source.list_directory()
            status.write_text("disconnected", encoding="utf-8")
            with self.assertRaisesRegex(StateError, "nie ukończyło"):
                source.list_directory()
            status.write_text("partial", encoding="utf-8")
            with source.read_chunks(Path("image.jpg")) as chunks:
                self.assertEqual(next(chunks), b"partial")
                with self.assertRaisesRegex(StateError, "przerwało"):
                    next(chunks)

    def test_adb_reads_pipe_above_select_descriptor_limit(self) -> None:
        limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if limit != resource.RLIM_INFINITY and limit < 1040:
            self.skipTest("File descriptor limit is too low for this regression test")
        self._fake_adb()
        path = Path("image.jpg")
        (self.android / path).write_bytes(b"synthetic media")

        with open_adb_source(self.config, "main") as source, ExitStack() as stack:
            with open(os.devnull, "rb") as placeholder:
                while True:
                    descriptor = os.dup(placeholder.fileno())
                    stack.callback(os.close, descriptor)
                    if descriptor >= 1023:
                        break
                with source.read_chunks(path) as chunks:
                    self.assertEqual(b"".join(chunks), b"synthetic media")

    def test_adb_hash_reports_progress_and_handles_empty_files(self) -> None:
        self._fake_adb()
        payload = b"\xff\x00\x0a\x0d" * 300_000
        path = Path("media.bin")
        (self.android / path).write_bytes(payload)
        (self.android / "empty").touch()
        updates = []
        with open_adb_source(self.config, "main") as source:
            digest = hash_source_file(source, path, progress=lambda done, total: updates.append((done, total)))
            self.assertEqual(digest, SourceDigest(sha256(payload).hexdigest(), len(payload)))
            self.assertEqual(hash_source_file(source, Path("empty")), SourceDigest(sha256(b"").hexdigest(), 0))
        self.assertEqual(updates[0], (0, len(payload)))
        self.assertEqual(updates[-1], (len(payload), len(payload)))
        self.assertGreater(len(updates), 2)
        self.assertEqual([done for done, _ in updates], sorted({done for done, _ in updates}))

    def test_adb_hash_rechecks_identity_after_reading(self) -> None:
        self._fake_adb()
        path = Path("media.bin")
        original = self.android / path
        original.write_bytes(b"original")
        replacement = self.android / "replacement.bin"
        replacement.write_bytes(b"replaced")
        observed = original.stat()
        os.utime(replacement, ns=(observed.st_atime_ns, observed.st_mtime_ns))

        def replace_after_last_chunk(done: int, total: int) -> None:
            if done == total:
                replacement.replace(original)

        with open_adb_source(self.config, "main") as source:
            with self.assertRaisesRegex(StateError, "zmienił się|przerwało"):
                hash_source_file(source, path, progress=replace_after_last_chunk)

    def test_adb_hash_reads_the_verified_descriptor_without_reopening_the_path(self) -> None:
        self._fake_adb()
        path = Path("media.bin")
        payload = b"original"
        (self.android / path).write_bytes(payload)
        (self.android / "replacement.bin").write_bytes(b"replaced")
        client_cat = self.root / "cat"
        client_cat.write_text(r'''#!/bin/sh
if [ "$1" = ./media.bin ]; then
    mv ./media.bin ./saved.bin && mv ./replacement.bin ./media.bin || exit 1
    "$SOURCE_TEST_CAT" "$@"
    result=$?
    mv ./saved.bin ./media.bin || exit 1
    exit "$result"
fi
exec "$SOURCE_TEST_CAT" "$@"
''', encoding="utf-8")
        client_cat.chmod(0o700)
        real_cat = shutil.which("cat", path=os.defpath)
        assert real_cat is not None
        with patch.dict(os.environ, {"SOURCE_TEST_CAT": real_cat}):
            with open_adb_source(self.config, "main") as source:
                self.assertEqual(hash_source_file(source, path), SourceDigest(sha256(payload).hexdigest(), len(payload)))

    def test_adb_hash_rejects_incomplete_streams_and_cancellation(self) -> None:
        status = self._fake_adb()
        path = Path("media.bin")
        (self.android / path).write_bytes(b"1234567")
        with open_adb_source(self.config, "main") as source:
            for response, diagnostic in (("partial", "przerwało"), ("short", "liczba bajtów")):
                status.write_text("device", encoding="utf-8")

                def interrupt_after_metadata(done: int, total: int) -> None:
                    if done == 0:
                        status.write_text(response, encoding="utf-8")

                with self.subTest(response=response), self.assertRaisesRegex(StateError, diagnostic):
                    hash_source_file(source, path, progress=interrupt_after_metadata)

            status.write_text("device", encoding="utf-8")
            read = 0

            def cancel_after_chunk(done: int, total: int) -> None:
                nonlocal read
                read = done

            with self.assertRaises(ReadCancelled):
                hash_source_file(source, path, progress=cancel_after_chunk, cancelled=lambda: read > 0)
            self.assertGreater(read, 0)
            self.assertEqual(hash_source_file(source, path).size, 7)

    def test_adb_verifies_registered_phone_and_blocks_rebinding_during_use(self) -> None:
        self._fake_adb()
        previous = next(item for item in load_registered_devices(self.config.app.database_file)
                        if item.key == DeviceKey("phone", "main"))
        self.values["PHONE_MAIN_SERIAL"] = "replacement-phone"
        write_config(self.settings, self.values)
        candidate = load_config(self.settings)
        with self.assertRaisesRegex(ConfigError, "jawnej rejestracji"), open_adb_source(candidate, "main"):
            pass
        with open_adb_source(self.config, "main"):
            with self.assertRaises(OperationBusy):
                register_configuration(candidate, device_changes=[DeviceChange(previous, "replace")])
