"""Synthetic source, staging and history checks for the shared import planner."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from media_archive.cli import main
from media_archive.config import load_config
from media_archive.devices import MountedVolume, _mount_id, registered_volume
from media_archive.import_planning import plan_import
from media_archive.sources import LocalSource
from media_archive.state import DeviceKey, StateError, open_database, transaction
from media_archive.state import history
from media_archive.state.registry import initialize_database
from tests.config_fixtures import registry_values, write_config


@contextmanager
def mounted(root: Path):
    fd = os.open(root, os.O_PATH | os.O_DIRECTORY)
    try:
        yield MountedVolume(root, _mount_id(fd), "synthetic", "8:1", "8:0", (), False, fd)
    finally:
        os.close(fd)


class ImportPlanningTests(unittest.TestCase):
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
        self.card = self.root / "card"
        self.camera_root = self.card / self.config.camera("hero8").source_relative
        self.camera_root.mkdir(parents=True)
        self.enterContext(patch("media_archive.import_planning.open_camera_source", side_effect=self._camera))
        self.enterContext(patch("media_archive.import_planning.verify_volume", side_effect=self._staging))

    @contextmanager
    def _camera(self, config, profile, card, mount):
        self.assertEqual((profile, card, mount), ("hero8", "a", self.card))
        with mounted(self.card) as volume:
            yield LocalSource(volume, self.camera_root)

    def _staging(self, config, key, *, writable):
        self.assertEqual(key, DeviceKey("local", self.config.local.volumes[0].alias))
        self.assertTrue(writable)
        return mounted(self.volume)

    def _plan(self, **kwargs):
        return plan_import(self.config, "hero8", card="a", mount=self.card, **kwargs)

    def _imported(self, data: bytes, *, phone: bool = False) -> int:
        device = registered_volume(self.config, DeviceKey("phone", "main") if phone else DeviceKey("card", "a"))
        with open_database(self.database, writable=True) as con, transaction(con):
            run = history.create_run(con)
            digest = sha256(data).hexdigest()
            content = history.record_content(con, digest, len(data))
            source = history.record_occurrence(con, "source", "/DCIM/old.jpg" if phone else "DCIM/old.jpg",
                                               content_id=content, device_id=device.device_id, volume_id=device.volume_id)
            staging = history.record_occurrence(con, "staging", str(self.root / "old.jpg"), content_id=content)
            proof = history.record_verification(con, staging, run, verified_at=datetime.now(timezone.utc),
                                                scope="full", read_sha256=digest, bytes_read=len(data))
            return history.record_import(con, source, proof)

    def test_manifest_hashes_content_and_isolates_history_by_profile(self) -> None:
        self._imported(b"cross", phone=True)
        self._imported(b"prior")
        for name, data in (("a.JPG", b"cross"), ("duplicate.jpg", b"cross"), ("prior.jpg", b"prior"),
                           ("old.jpg", b"fresh"), ("sidecar.LRV", b"x"), ("unknown.wav", b"x")):
            (self.camera_root / name).write_bytes(data)
        before = self.database.read_bytes()
        preview = self._plan()
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse(self.config.local.staging_root.exists())
        self.assertEqual({item.relative_path.name: item.decision for item in preview.items},
                         {"a.JPG": "copy", "duplicate.jpg": "duplicate", "prior.jpg": "already_imported",
                          "old.jpg": "copy", "sidecar.LRV": "ignored", "unknown.wav": "unsupported"})
        recorded = self._plan(record=True)
        self.assertEqual([(item.relative_path, item.destination) for item in recorded.transfers],
                         [(item.relative_path, item.destination) for item in preview.transfers])
        with open_database(self.database) as con:
            observed = history.load_observed_contents(con, recorded.observation_run_id, "source")
            self.assertEqual(len(observed), 4)
            self.assertTrue(all(row.path.startswith("DCIM/") and row.volume_id is not None for row in observed))
            self.assertEqual(con.exec_driver_sql("SELECT count(*) FROM main.history_imports").scalar_one(), 2)

    def test_explicit_reimport_uses_exact_path_without_overwriting_staging(self) -> None:
        prior = self._imported(b"same")
        for filename in ("other.jpg", "selected.jpg"):
            (self.camera_root / filename).write_bytes(b"same")
        (self.camera_root / "new.jpg").write_bytes(b"brand new")
        plan = self._plan(reimport=["selected.jpg"])
        selected = next(item for item in plan.transfers if item.relative_path == Path("selected.jpg"))
        self.assertEqual(selected.decision, "reimport")
        self.assertIn(f"reimport-{prior}", selected.destination.parts)
        self.assertFalse(self._plan(reimport=["other-missing.jpg"]).ready)
        self.assertFalse(self._plan(reimport=["new.jpg"]).ready)
        selected.destination.parent.mkdir(parents=True)
        selected.destination.write_bytes(b"unrelated")
        blocked = self._plan(reimport=["selected.jpg"], record=True)
        self.assertFalse(blocked.ready)
        self.assertEqual(blocked.transfers, ())
        self.assertIsNone(blocked.observation_run_id)
        self.assertEqual(selected.destination.read_bytes(), b"unrelated")

    def test_capacity_includes_all_allocated_blocks_and_the_reserve(self) -> None:
        (self.camera_root / "one.jpg").write_bytes(b"one")
        (self.camera_root / "two.mp4").write_bytes(b"two")
        reserve = self.config.app.min_free_bytes
        # Two files, two work directories, and five unique missing parents.
        required = 9 * 4096
        with patch("media_archive.import_planning.Destination.space", return_value=(reserve + required - 1, 4096)):
            blocked = self._plan(record=True)
        self.assertEqual(blocked.required_bytes, required)
        self.assertTrue(any("Za mało" in error for error in blocked.problems))
        self.assertIsNone(blocked.observation_run_id)
        self.assertFalse(self.config.local.staging_root.exists())
        with patch("media_archive.import_planning.Destination.space", return_value=(reserve + required, 4096)):
            self.assertEqual(len(self._plan().transfers), 2)

    @unittest.skipIf(os.geteuid() == 0, "requires ordinary directory permissions")
    def test_preflight_checks_where_the_first_creation_would_happen(self) -> None:
        (self.camera_root / "copy.jpg").write_bytes(b"sample")
        initial = self._plan()
        destination = initial.transfers[0].destination
        staging = self.config.local.staging_root
        staging.mkdir()
        for directory in (staging, destination.parent):
            with self.subTest(directory=directory):
                directory.mkdir(parents=True, exist_ok=True)
                directory.chmod(0o500)
                try:
                    plan = self._plan()
                    self.assertFalse(plan.ready)
                    self.assertTrue(any("Brak prawa zapisu" in error for error in plan.problems))
                    self.assertEqual(plan.transfers, ())
                    self.assertFalse(destination.exists())
                finally:
                    directory.chmod(0o700)

    def test_staging_uses_registered_source_id_instead_of_alias(self) -> None:
        (self.camera_root / "copy.jpg").write_bytes(b"sample")
        for index, alias in enumerate(("..", ".media-archive-transfer-" + "a" * 32)):
            with self.subTest(alias=alias):
                values = registry_values(self.root)
                state = self.root / f"alias-state-{index}"
                state.mkdir()
                values.update(APP_STATE_DIR=str(state), CAMERA_HERO8_ALIAS=alias)
                write_config(self.settings, values)
                config = load_config(self.settings)
                initialize_database(config)
                with patch("media_archive.import_planning.open_camera_source",
                           return_value=self._camera(config, "hero8", "a", self.card)):
                    plan = plan_import(config, alias, card="a", mount=self.card)
                item, = plan.transfers
                directory = item.destination.relative_to(config.local.staging_root)
                self.assertEqual(directory.parts[:2], ("camera", str(plan.source_id)))
                self.assertNotIn(alias, directory.parts)

    def test_file_read_failure_blocks_batch_even_with_other_candidates(self) -> None:
        for name in ("good.jpg", "bad.jpg"):
            (self.camera_root / name).write_bytes(name.encode())
        from media_archive.sources import hash_source_file

        def read(source, path, **kwargs):
            if path == Path("bad.jpg"):
                raise StateError("Testowy błąd źródła.")
            return hash_source_file(source, path, **kwargs)

        with patch("media_archive.import_planning.hash_source_file", side_effect=read):
            plan = self._plan(record=True)
        self.assertFalse(plan.ready)
        self.assertEqual(plan.transfers, ())
        self.assertIsNone(plan.observation_run_id)
        self.assertIn("unreadable", [item.decision for item in plan.items])

    def test_cli_dry_run_writes_only_the_requested_report(self) -> None:
        (self.camera_root / "copy.jpg").write_bytes(b"sample")
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = main(["--config", str(self.settings), "import", "--source", "hero8", "--card", "a",
                         "--mount", str(self.card), "--dry-run", "--report"])
        self.assertEqual(code, 0, out.getvalue())
        report, = self.config.app.reports_dir.glob("*/report.json")
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(data["tryb_planowania"])
        self.assertEqual(data["szczegóły"]["Manifest importu"][0]["decyzja"], "do skopiowania")
        self.assertFalse(self.config.local.staging_root.exists())
        with open_database(self.database) as con:
            self.assertEqual(con.exec_driver_sql("SELECT count(*) FROM main.history_observations").scalar_one(), 0)
