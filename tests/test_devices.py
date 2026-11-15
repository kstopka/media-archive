"""Exercise Linux volume probes and explicit registration with synthetic block devices."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from media_archive.cli import main
from media_archive.config import Configuration, load_config
from media_archive.devices import _mount_id, open_media_file, verify_volume
from media_archive.state import DeviceChange, DeviceKey, RegisteredDevice, StateError, load_registered_devices, open_database
from media_archive.state._installation import OperationBusy
from media_archive.state.registry import change_registered_volume, initialize_database, register_configuration
from tests.config_fixtures import registry_values, write_config


class DeviceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = self.root / ".env"
        self.values = registry_values(self.root)
        self.card_key = "CAMERA_CARD_A_UUID"
        self.card_uuid = self.values[self.card_key]
        self.config = self._config()
        self.database = self.config.app.database_file
        self.database.parent.mkdir(parents=True)
        initialize_database(self.config)
        self.card = DeviceKey("card", "a")
        self.mount = self.root / "card"
        self.mount.mkdir()
        descriptor = os.open(self.mount, os.O_RDONLY | os.O_DIRECTORY)
        try:
            ident = _mount_id(descriptor)
        finally:
            os.close(descriptor)
        self.mount_rows = [{
            "id": ident, "fsroot": "/", "source": "/dev/test1", "target": str(self.mount),
            "options": "rw,noexec", "maj:min": "8:1", "fstype": "exfat",
        }]
        self.block_rows = [
            {"path": "/dev/test", "kname": "test", "maj:min": "8:0", "type": "disk",
             "uuid": None, "wwn": None, "serial": "synthetic-original", "ro": False},
            {"path": "/dev/test1", "kname": "test1", "pkname": "test", "maj:min": "8:1",
             "type": "part", "uuid": self.card_uuid, "ro": False},
        ]
        self.probe = self.enterContext(patch("media_archive.devices.subprocess.run", side_effect=self._probe))

    def _config(self) -> Configuration:
        write_config(self.settings, self.values)
        return load_config(self.settings)

    def _probe(self, args: list[str], **options: object) -> CompletedProcess[str]:
        self.assertEqual(options["encoding"], "utf-8")
        self.assertIn("--json", args)
        if args[0] == "findmnt":
            if not self.mount_rows:
                return CompletedProcess(args, 1, "", "")
            return CompletedProcess(args, 0, json.dumps({"filesystems": self.mount_rows}), "")
        self.assertEqual(args[0], "lsblk")
        return CompletedProcess(args, 0, json.dumps({"blockdevices": self.block_rows}), "")

    def _registered(self) -> RegisteredDevice:
        return next(item for item in load_registered_devices(self.database) if item.key == self.card)

    def _command(self, *arguments: str, expected: int = 0) -> str:
        output, error = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            result = main(["--config", str(self.settings), "device", *arguments])
        self.assertEqual(result, expected, error.getvalue())
        return output.getvalue() + error.getvalue()

    def test_check_requires_mounted_unique_expected_uuid(self) -> None:
        self._command("check", "card:a", "--to", str(self.mount))
        self.assertIn("--mountpoint", self.probe.call_args_list[0].args[0])
        self.block_rows.append({**self.block_rows[1], "path": "/dev/copy1", "maj:min": "9:1"})
        self.assertIn("niejednoznaczny UUID", self._command("check", "card:a", "--to", str(self.mount), expected=1))
        self.block_rows.pop()
        self.block_rows[1]["uuid"] = "wrong-uuid"
        self.assertIn("nie zgadza się", self._command("check", "card:a", "--to", str(self.mount), expected=1))
        self.mount_rows.clear()
        self.assertTrue(self.mount.is_dir())
        self.assertIn("nie ma oczekiwanego montowania",
                      self._command("check", "card:a", "--to", str(self.mount), expected=1))

    def test_local_root_may_be_below_mount_and_archive_read_only_cannot_be_written(self) -> None:
        local = self.config.local.volumes[0]
        local.root.mkdir()
        self.mount_rows[0]["target"] = str(local.root.parent)
        self.block_rows[1]["uuid"] = local.uuid
        with verify_volume(self.config, DeviceKey("local", local.alias)) as volume:
            self.assertEqual(volume.root, local.root)
        self.assertIn("--target", self.probe.call_args_list[0].args[0])

        group = self.config.archive_groups[0]
        archived = group.targets[0]
        archived.mount.mkdir()
        self.mount_rows[0]["target"] = str(archived.mount)
        self.block_rows[1]["uuid"] = archived.uuid
        key = DeviceKey("archive", archived.alias, group.id)
        with self.assertRaisesRegex(StateError, "Zapis wymaga"), verify_volume(self.config, key, writable=True):
            self.fail("an archive without hardware evidence was accepted for write")
        self._command("register", f"archive:{group.id}:{archived.alias}", "--yes")
        with verify_volume(self.config, key, writable=True) as volume:
            self.assertFalse(volume.read_only)
        self.mount_rows[0]["options"] = "ro,noexec"
        with self.assertRaisesRegex(StateError, "uprawnień"), verify_volume(self.config, key, writable=True):
            self.fail("a read-only mount was accepted for write")

        self.mount_rows[0]["options"] = "rw,noexec"
        self.values[f"ARCHIVE_{group.id.upper()}_TARGET_{archived.alias.upper()}_STATE"] = "read_only"
        config = self._config()
        with verify_volume(config, key) as volume:
            self.assertTrue(volume.read_only)
        with self.assertRaisesRegex(StateError, "Rola nośnika"), verify_volume(config, key, writable=True):
            self.fail("read-only archive was accepted for write")

    def test_secure_open_keeps_scope_and_rejects_links_and_special_files(self) -> None:
        scope = self.mount / "scope"
        scope.mkdir()
        media = scope / "żółć.jpg"
        media.write_bytes(b"synthetic")
        (self.root / "outside").write_bytes(b"outside")
        (scope / "link").symlink_to(self.root / "outside")
        (scope / "alias").symlink_to(scope, target_is_directory=True)
        os.mkfifo(scope / "pipe")
        with verify_volume(self.config, self.card, self.mount) as volume:
            with open_media_file(volume, Path(media.name), scope=scope) as handle:
                self.assertEqual(handle.read(), b"synthetic")
            for relative in ("../scope/żółć.jpg", "link", "alias/żółć.jpg", "pipe", "bad\udcff"):
                with self.subTest(relative=relative), self.assertRaises(StateError):
                    open_media_file(volume, Path(relative), scope=scope)
            with self.assertRaises(StateError):
                open_media_file(volume, Path("outside"), scope=self.root)
            with patch("media_archive.devices._mount_id", return_value=volume.mount_id + 1):
                with self.assertRaisesRegex(StateError, "Montowanie zmieniło"):
                    open_media_file(volume, Path(media.name), scope=scope)

    def test_explicit_registration_reformat_and_replace_preserve_old_episodes(self) -> None:
        before = self._registered()
        arguments = ("register", "card:a", "--to", str(self.mount))
        database_bytes = self.database.read_bytes()
        self.assertIn("Powtórz z --yes", self._command(*arguments))
        self.assertEqual(self.database.read_bytes(), database_bytes)
        self._command(*arguments, "--yes")
        self.assertEqual(self._registered().hardware_id, "serial:synthetic-original")
        self.block_rows[0]["serial"] = "synthetic-other"
        self.assertIn("fizycznego", self._command("check", "card:a", "--to", str(self.mount), expected=1))
        self.block_rows[0]["serial"] = "synthetic-original"

        self.values[self.card_key] = self.block_rows[1]["uuid"] = "new-filesystem"
        self._config()
        self._command("reformat", "card:a", "--to", str(self.mount), "--yes")
        reformatted = self._registered()
        self.assertEqual(reformatted.device_id, before.device_id)
        self.assertNotEqual(reformatted.volume_id, before.volume_id)

        self.values[self.card_key] = self.block_rows[1]["uuid"] = "replacement-filesystem"
        self._config()
        self._command("replace", "card:a", "--to", str(self.mount), "--yes", expected=2)
        self.assertEqual(self._registered(), reformatted)
        self.block_rows[0]["serial"] = "synthetic-replacement"
        self._command("replace", "card:a", "--to", str(self.mount), "--yes")
        replaced = self._registered()
        self.assertNotEqual(replaced.device_id, before.device_id)
        self.assertEqual(replaced.hardware_id, "serial:synthetic-replacement")
        with open_database(self.database) as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT uuid FROM main.registry_volume_episodes WHERE device_id = ? ORDER BY id",
                (before.device_id,),
            ).scalars().all(), [self.card_uuid, "new-filesystem"])

    def test_verified_volume_excludes_replacement_during_probe_and_use(self) -> None:
        previous = self._registered()
        replacement_uuid = "synthetic-replacement"

        def probe_with_replacement(args: list[str], **options: object) -> CompletedProcess[str]:
            if args[0] == "lsblk":
                with self.assertRaises(OperationBusy):
                    change_registered_volume(self.database, previous, "replace", replacement_uuid, None, dry_run=False)
            return self._probe(args, **options)

        self.probe.side_effect = probe_with_replacement
        self.values[self.card_key] = replacement_uuid
        replacement_config = self._config()
        approval = DeviceChange(previous, "replace")
        with verify_volume(self.config, self.card, self.mount):
            with self.assertRaises(OperationBusy):
                register_configuration(replacement_config, device_changes=[approval])
            self.assertEqual(self._registered(), previous)

        register_configuration(replacement_config, device_changes=[approval])
        self.assertEqual(self._registered().uuid, replacement_uuid)

    def test_device_command_rejects_state_inside_card_mount_before_opening_database(self) -> None:
        self.values["APP_STATE_DIR"] = str(self.mount / "app-state")
        config = self._config()
        config.app.state_dir.mkdir()
        initialize_database(config)
        database = config.app.database_file
        before = database.read_bytes()

        with patch("media_archive.devices.load_registered_devices", side_effect=AssertionError("unexpected database access")):
            output = self._command("register", "card:a", "--to", str(self.mount), "--yes", expected=1)
        self.assertIn("Katalog stanu koliduje", output)
        self.assertEqual(database.read_bytes(), before)
