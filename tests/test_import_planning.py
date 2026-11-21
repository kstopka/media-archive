"""Synthetic source, staging and history checks for the shared import planner."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from media_archive.cli import main
from media_archive.camera_import import import_camera
from media_archive.config import load_config
from media_archive.devices import MountedVolume, _mount_id, registered_volume
from media_archive.import_planning import plan_import
from media_archive.sources import LocalSource
from media_archive.state import DeviceChange, DeviceKey, StateError, open_database, register_configuration, transaction
from media_archive.state import history, journal
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
        self.enterContext(patch("media_archive.camera_import.open_camera_source", side_effect=self._camera))
        self.enterContext(patch("media_archive.transfer.verify_volume", side_effect=self._staging))

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

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_camera_cli_imports_only_verified_videos_and_photos(self) -> None:
        files = {"clip.MP4": b"video", "photo.JPG": b"photo", "temp.THM": b"thumbnail",
                 "clip.LRV": b"preview", "unhandled.WAV": b"audio"}
        for name, data in files.items():
            (self.camera_root / name).write_bytes(data)
        preview = self._plan()
        report, terminal = self._live_cli()
        self.assertEqual(report["Zaimportowano plików"], 2)
        self.assertIn("Nieobsługiwane wpisy · 1", terminal)
        self.assertEqual(report["Pominięte formaty"], 2)
        self.assertEqual({item.destination: item.destination.read_bytes() for item in preview.transfers},
                         {item.destination: files[item.relative_path.name] for item in preview.transfers})
        self.assertEqual({path.name: path.read_bytes() for path in self.camera_root.iterdir()}, files)
        with open_database(self.database) as con:
            profile = history.load_source_id(con, "camera", "hero8")
            imports = history.load_imports(con, profile)
            self.assertEqual(len(imports), 2)
            for result in report["Wyniki importu"]:
                attempt, = journal.load_attempts(con, result["operacja"])
                self.assertEqual(attempt.state, "committed")
            self.assertEqual(con.exec_driver_sql(
                "SELECT count(*) FROM main.history_verifications WHERE result = 'match'"
            ).scalar_one(), 2)

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_camera_import_preserves_content_history_after_card_reformat(self) -> None:
        clip = self.camera_root / "clip.MP4"
        clip.write_bytes(b"first")
        self.assertEqual(import_camera(self.config, "hero8", card="a", mount=self.card).code, 0)
        previous = registered_volume(self.config, DeviceKey("card", "a"))
        values = registry_values(self.root)
        values["CAMERA_CARD_A_UUID"] = "synthetic-reformatted-card"
        write_config(self.settings, values)
        self.config = load_config(self.settings)
        register_configuration(self.config, device_changes=[DeviceChange(previous, "reformat")])
        clip.write_bytes(b"other")  # Same name and length, but different bytes.
        (self.camera_root / "renamed.MP4").write_bytes(b"first")
        report, _ = self._live_cli()
        self.assertEqual(report["Zaimportowano plików"], 1)
        self.assertEqual({item["ścieżka"]: item["decyzja"] for item in report["Manifest importu"]},
                         {"clip.MP4": "do skopiowania", "renamed.MP4": "już zaimportowano"})
        with open_database(self.database) as con:
            profile = history.load_source_id(con, "camera", "hero8")
            imports = history.load_imports(con, profile)
            self.assertEqual(len(imports), 2)
            self.assertEqual(len({history.load_occurrence(con, row.source_occurrence_id).volume_id
                                  for row in imports}), 2)
            self.assertEqual({Path(history.load_occurrence(con, row.staging_occurrence_id).path).read_bytes()
                              for row in imports}, {b"first", b"other"})

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_camera_import_does_not_commit_changed_file_after_planning(self) -> None:
        (self.camera_root / "a.jpg").write_bytes(b"first")
        changed = self.camera_root / "b.MP4"
        changed.write_bytes(b"video")
        (self.camera_root / "c.jpg").write_bytes(b"later")

        def open_after_change(*args, **kwargs):
            changed.write_bytes(b"other")
            return self._camera(*args, **kwargs)

        with patch("media_archive.camera_import.open_camera_source", side_effect=open_after_change):
            report, _ = self._live_cli(expected=3)
        self.assertEqual(report["Zaimportowano plików"], 1)
        self.assertEqual([item["stan"] for item in report["Wyniki importu"]], ["zaimportowano", "błąd"])
        self.assertIn("SHA-256", report["Wyniki importu"][1]["błąd"])
        manifest = {item["ścieżka"]: Path(item["staging"]) for item in report["Manifest importu"]}
        self.assertEqual(manifest["a.jpg"].read_bytes(), b"first")
        self.assertFalse(manifest["b.MP4"].exists())
        self.assertFalse(manifest["c.jpg"].exists())
        with open_database(self.database) as con:
            profile = history.load_source_id(con, "camera", "hero8")
            self.assertEqual(len(history.load_imports(con, profile)), 1)

    @unittest.skipUnless(shutil.which("rsync"), "requires rsync")
    def test_camera_retry_reuses_destination_operation_after_source_moves(self) -> None:
        original = self.camera_root / "old-folder"
        original.mkdir()
        (original / "clip.MP4").write_bytes(b"same video")
        with patch("media_archive._transfer_files.Part.publish",
                   side_effect=StateError("Testowe przerwanie przed publikacją.")):
            first = import_camera(self.config, "hero8", card="a", mount=self.card)
        self.assertEqual(first.code, 3)
        failed, = first.results
        destination = first.plan.transfers[0].destination
        self.assertFalse(destination.exists())
        with open_database(self.database) as con:
            self.assertEqual(journal.load_attempts(con, failed.operation_id)[-1].state, "needs_review")

        original.rename(self.camera_root / "new-folder")
        retry = import_camera(self.config, "hero8", card="a", mount=self.card)
        self.assertEqual(retry.code, 0)
        copied, = retry.results
        self.assertEqual(copied.operation_id, failed.operation_id)
        self.assertEqual(copied.path, Path("new-folder/clip.MP4"))
        self.assertEqual(destination.read_bytes(), b"same video")
        with open_database(self.database) as con:
            self.assertEqual(journal.load_unfinished(con), ())
            profile = history.load_source_id(con, "camera", "hero8")
            imported, = history.load_imports(con, profile)
            self.assertTrue(history.load_occurrence(con, imported.source_occurrence_id).path.endswith(
                "/new-folder/clip.MP4"
            ))

    def _live_cli(self, *, expected: int = 0) -> tuple[dict, str]:
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = main(["--config", str(self.settings), "import", "--source", "hero8", "--card", "a",
                         "--mount", str(self.card)])
        self.assertEqual(code, expected, out.getvalue())
        report = sorted(self.config.app.reports_dir.glob("*/report.json"))[-1]
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertFalse(data["tryb_planowania"])
        return data["szczegóły"], out.getvalue()
