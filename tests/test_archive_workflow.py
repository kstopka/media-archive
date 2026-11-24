"""Run the archive workflow through the CLI with synthetic mounted volumes."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from media_archive._transfer_files import Destination, Part
from media_archive.backup_snapshots import preflight_snapshots
from media_archive.catalog import search_catalog
from media_archive.cli import main
from media_archive.config import load_config
from media_archive.devices import VolumeUnavailable
from media_archive.state import open_database
from media_archive.state.reconciliation import load_reconciliation
from tests.config_fixtures import registry_values, write_config
from tests.test_import_planning import mounted
from tests.test_sort_planning import _movie


@unittest.skipUnless(shutil.which("rsync") and shutil.which("exiftool"), "requires rsync and ExifTool")
class ArchiveWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = self.root / "settings.env"
        write_config(self.settings, registry_values(self.root))
        self.config = load_config(self.settings)
        self.card = self.root / "card"
        self.local, = self.config.local.volumes
        self.group = self.config.archive_group("video")
        self.targets = {disk.alias: disk.mount for disk in self.group.targets}
        self.archives = {alias: mount / self.group.root_relative for alias, mount in self.targets.items()}
        self.devices = {
            self.card: (self.config.camera("hero8").cards[0].uuid, "card"),
            self.local.root: (self.local.uuid, "working"),
            **{disk.mount: (disk.uuid, disk.alias) for disk in self.group.targets},
        }
        self.offline: set[Path] = set()
        for path in self.devices:
            path.mkdir()
        self.movie = self.card / self.config.camera("hero8").source_relative / "GX010001.MP4"
        self.movie.parent.mkdir()
        self.original = _movie(datetime(2024, 12, 31, 23, 30, tzinfo=timezone.utc))
        self.movie.write_bytes(self.original)
        self.placed = self.config.collection("gopro").local_root / "Różne 2025/2025-01-01/HERO8 Black 1" / self.movie.name
        self.enterContext(patch("media_archive.devices._inspect", side_effect=self._inspect))
        self._cli("setup", "--yes")
        for binding in ("local:working", "archive:video:a", "archive:video:b"):
            self._cli("device", "register", binding, "--yes")

    def _inspect(self, stack, settings, *, writable):
        if settings.root not in self.devices or settings.root in self.offline:
            raise VolumeUnavailable("Testowy nośnik odłączony.")
        uuid, disk = self.devices[settings.root]
        self.assertEqual(settings.uuid, uuid)
        self.assertFalse(writable and settings.read_only)
        volume = stack.enter_context(mounted(settings.root))
        return replace(volume, uuid=uuid, physical_device=disk, hardware_ids=(f"serial:{disk}",),
                       read_only=settings.read_only)

    def _cli(self, *args, expected=0):
        before = set(self.config.app.reports_dir.glob("*/report.json"))
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = main(["--config", str(self.settings), *map(str, args)])
        self.assertEqual(code, expected, output.getvalue())
        reports = set(self.config.app.reports_dir.glob("*/report.json")) - before
        if reports:
            report, = reports
            return json.loads(report.read_text(encoding="utf-8"))["szczegóły"]

    def _import(self, *, expected=0):
        return self._cli("import", "--source", "hero8", "--card", "a", "--mount", self.card, expected=expected)

    def _backup(self, *, expected=0):
        return self._cli("backup", "--snapshot-test", "--profile", "video", "--collection", "gopro",
                         "--targets", "a", "b", expected=expected)

    def _archive_copy(self, alias, source):
        return self.archives[alias] / self.config.collection("gopro").archive_relative / source.relative_to(
            self.config.collection("gopro").local_root)

    def _snapshot_root(self, alias):
        return self.targets[alias] / self.config.app.archive_state_dir

    def test_import_through_restore_preserves_manual_selection_and_archive_copies(self) -> None:
        self.assertEqual(self._import()["Zaimportowano plików"], 1)
        self.assertEqual(self._cli("sort")["Przeniesiono do zbioru"], 1)
        self.assertEqual(self.placed.read_bytes(), self.original)
        local, = [row for row in search_catalog(self.config, source=("camera", "hero8")) if row["rodzaj"] == "local"]
        selected = self.placed.with_name("selected.MP4")
        self.placed.rename(selected)
        self._cli("catalog-move", local["id"], selected)
        self.assertEqual(self._import()["Zaimportowano plików"], 0)

        for archive in self.archives.values():
            old = archive / "GoPro" / "retained.MP4"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"archive-only material")
        backup = self._backup()
        copy_status, = backup["Stan kopii BACKUP"]
        self.assertEqual(copy_status["status kopii"], "2/2")
        self.assertEqual(backup["Plan usunięć"], [])
        name, = {row["generacja"] for row in backup["Snapshoty BACKUP"]}
        generations = [self._snapshot_root(alias) / name for alias in self.targets]
        for generation in generations:
            self._cli("verify-snapshot", generation)
        for filename in ("state.sqlite3", "settings.env", "manifest.json"):
            self.assertEqual((generations[0] / filename).read_bytes(), (generations[1] / filename).read_bytes())
        for alias, archive in self.archives.items():
            self.assertEqual(self._archive_copy(alias, selected).read_bytes(), self.original)
            self.assertFalse(self._archive_copy(alias, self.placed).exists())
            self.assertEqual((archive / "GoPro" / "retained.MP4").read_bytes(), b"archive-only material")

        audit = self._cli("audit", "--mode", "full", "--group", "video", "--targets", "a", "b")
        self.assertEqual(audit["Zgodne pliki SHA-256"], 2)
        self.assertEqual(audit["Liczba różnic"], 0)
        self._cli("restore", generations[0], "--yes")
        with open_database(self.config.app.database_file) as connection:
            self.assertIsNotNone(load_reconciliation(connection))
        reconciliation = self._cli("reconcile")
        self.assertEqual(reconciliation["Sprawdzone ścieżki BACKUP"], 2)
        self.assertEqual(reconciliation["Brakujące ścieżki BACKUP"], 0)
        with open_database(self.config.app.database_file) as connection:
            self.assertIsNone(load_reconciliation(connection))
        locations = search_catalog(self.config, name=selected.name, source=("camera", "hero8"))
        self.assertEqual({row["rodzaj"] for row in locations}, {"local", "archive"})
        self.assertEqual(len(locations), 3)
        self.assertEqual({row["sha256"] for row in locations}, {sha256(self.original).hexdigest()})
        self.assertEqual(self.movie.read_bytes(), self.original)
        self.assertEqual(selected.read_bytes(), self.original)
        for alias in self.targets:
            with self.subTest(target=alias):
                self.assertEqual(self._archive_copy(alias, selected).read_bytes(), self.original)

    def test_recovers_publication_and_disconnected_target_after_capacity_block(self) -> None:
        publish = Part.publish

        def interrupt_after_publish(part, *args):
            publish(part, *args)
            raise KeyboardInterrupt

        with patch.object(Part, "publish", interrupt_after_publish):
            interrupted = self._import(expected=130)
        self.assertEqual(interrupted["Zaimportowano plików"], 0)
        operation, = interrupted["Wyniki importu"]
        self.offline.add(self.card)
        with patch.object(Part, "rsync_from", side_effect=AssertionError("staging file is already published")):
            self._cli("resume", operation["operacja"])
        self.assertEqual(self._cli("sort")["Przeniesiono do zbioru"], 1)

        space = Destination.space

        def insufficient(destination):
            if destination.volume.root == self.targets["b"]:
                return 0, 4096
            return space(destination)

        with patch.object(Destination, "space", insufficient):
            blocked = self._backup(expected=3)
        self.assertTrue(any(row["cel"] == "b" and row["brakuje"] for row in blocked["Pojemność BACKUP"]))
        for mount in self.targets.values():
            self.assertEqual(list(mount.iterdir()), [])

        def unplug_after_preflight(*args):
            result = preflight_snapshots(*args)
            self.assertFalse(result[2])
            self.offline.add(self.targets["b"])
            return result

        with patch("media_archive.backup_execution.preflight_snapshots", unplug_after_preflight):
            partial = self._backup(expected=3)
        copy_status, = partial["Stan kopii BACKUP"]
        self.assertEqual(copy_status["status kopii"], "1/2")
        self.assertEqual([row["cel"] for row in copy_status["kopie"]], ["a"])
        snapshot_errors = [row["cel"] for row in partial["Snapshoty BACKUP"] if "błąd" in row]
        self.assertEqual(snapshot_errors, ["b"])
        self.assertEqual(list(self.targets["b"].iterdir()), [])
        a_copy = self._archive_copy("a", self.placed)
        before = a_copy.stat()
        self.offline.remove(self.targets["b"])
        recovered = self._backup()
        copy_status, = recovered["Stan kopii BACKUP"]
        self.assertEqual(copy_status["status kopii"], "2/2")
        self.assertEqual((a_copy.stat().st_ino, a_copy.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        for alias in self.targets:
            self.assertEqual(self._archive_copy(alias, self.placed).read_bytes(), self.original)
        self.assertEqual(self.placed.read_bytes(), self.movie.read_bytes())
