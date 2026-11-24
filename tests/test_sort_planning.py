"""Exercise SORT previews with verified staging and synthetic media."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
import errno
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import shutil
from struct import pack
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
import zlib

from media_archive.cli import main
from media_archive.catalog import list_conflicts, search_catalog
from media_archive._transfer_files import Destination, Part
from media_archive.config import load_config
from media_archive.devices import registered_volume
from media_archive.metadata import MetadataToolError, read_metadata
from media_archive.import_restore import reconcile_imports
from media_archive.sort_execution import execute_sort, resume_sort
from media_archive.sort_planning import plan_sort, set_sort_date
from media_archive.state import DeviceKey, StateError, history, open_database, sort_moves, transaction
from media_archive.state.registry import initialize_database
from media_archive.state.reconciliation import load_reconciliation
from media_archive.state.snapshots import create_snapshot, restore_snapshot
from tests.config_fixtures import registry_values, write_config
from tests.test_import_planning import mounted


def _movie(value: datetime) -> bytes:
    def atom(tag: bytes, body: bytes) -> bytes:
        return pack(">I", len(body) + 8) + tag + body

    stamp = int(value.timestamp()) + 2082844800
    header = pack(">5I", 0, stamp, stamp, 1000, 1000) + bytes(80)
    return atom(b"ftyp", b"isom" + bytes(4) + b"isom") + atom(b"moov", atom(b"mvhd", header))


def _photo() -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        return pack(">I", len(body)) + tag + body + pack(">I", zlib.crc32(tag + body))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(2))) + chunk(b"IEND", b""))


class SortPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = self.root / "settings.env"
        write_config(self.settings, registry_values(self.root))
        self.config = load_config(self.settings)
        self.database = self.config.app.database_file
        self.database.parent.mkdir(parents=True)
        initialize_database(self.config)
        self.volume = self.config.local.volumes[0]
        self.volume.root.mkdir(parents=True)
        for module in ("sort_planning", "sort_execution", "transfer", "import_restore", "catalog"):
            self.enterContext(patch(f"media_archive.{module}.verify_volume",
                                    side_effect=lambda *_, **__: mounted(self.volume.root)))

    def _import(self, path: Path, data: bytes, *, phone: bool = True) -> tuple[int, Path]:
        key = DeviceKey("phone", "main") if phone else DeviceKey("card", "a")
        device = registered_volume(self.config, key)
        volume = registered_volume(self.config, DeviceKey("local", self.volume.alias))
        digest = sha256(data).hexdigest()
        destination = self.config.local.staging_root / digest / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        with open_database(self.database, writable=True) as connection, transaction(connection):
            run = history.create_run(connection)
            content = history.record_content(connection, digest, len(data))
            source = history.record_occurrence(connection, "source", str(path), content_id=content,
                                               device_id=device.device_id, volume_id=device.volume_id)
            staged = history.record_occurrence(connection, "staging", str(destination), content_id=content,
                                               volume_id=volume.volume_id)
            proof = history.record_verification(connection, staged, run, verified_at=datetime.now(timezone.utc),
                                                scope="full", read_sha256=digest, bytes_read=len(data))
            return history.record_import(connection, source, proof), destination

    def _cli(self, *args: str, expected: int = 0) -> str:
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = main(["--config", str(self.settings), *args])
        self.assertEqual(code, expected, output.getvalue())
        return output.getvalue()

    @unittest.skipUnless(shutil.which("exiftool"), "requires ExifTool")
    def test_preview_uses_recorded_photo_day_and_local_video_day_without_writes(self) -> None:
        photo = self.root / "photo.png"
        photo.write_bytes(_photo())
        subprocess.run(["exiftool", "-config", "", "-overwrite_original",
                        "-EXIF:DateTimeOriginal=2024:12:31 23:59:59", str(photo)], check=True, capture_output=True)
        original = photo.read_bytes()
        _, staged_photo = self._import(Path("/camera/photo.png"), original)
        first = _movie(datetime(2024, 12, 31, 22, 58, tzinfo=timezone.utc))
        second = _movie(datetime(2024, 12, 31, 23, 30, tzinfo=timezone.utc))
        self._import(Path("DCIM/GX010001.MP4"), first, phone=False)
        self._import(Path("DCIM/GX020001.MP4"), second, phone=False)
        unchanged = self.database.read_bytes()
        self._cli("sort", "--dry-run", "--report")
        report, = self.config.app.reports_dir.glob("*/report.json")
        rows = json.loads(report.read_text(encoding="utf-8"))["szczegóły"]["Plan SORT"]
        by_name = {row["plik"]: row for row in rows}
        self.assertTrue(by_name["photo.png"]["cel"].endswith("/2024/12.2024/photo.png"))
        self.assertTrue(by_name["GX010001.MP4"]["cel"].endswith("/Różne 2024/2024-12-31/HERO8 Black 1/GX010001.MP4"))
        self.assertTrue(by_name["GX020001.MP4"]["cel"].endswith("/Różne 2025/2025-01-01/HERO8 Black 1/GX020001.MP4"))
        self.assertEqual(by_name["GX020001.MP4"]["metadane"]["surowa_data"], "2024:12:31 23:30:00+00:00")
        self.assertEqual(self.database.read_bytes(), unchanged)
        self.assertEqual(staged_photo.read_bytes(), original)
        self.assertFalse(self.config.collection("gopro").local_root.exists())

    @unittest.skipUnless(shutil.which("exiftool"), "requires ExifTool")
    def test_manual_date_replans_a_file_in_do_wyjasnienia(self) -> None:
        import_id, staging = self._import(Path("/camera/photo.png"), _photo())
        first, = plan_sort(self.config)
        self.assertEqual(first.state, "undated")
        self.assertTrue(first.target.is_relative_to(self.config.local.undated_root))
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            main(["--config", str(self.settings), "sort-date", str(import_id), "2025-02-29"])
        with self.assertRaisesRegex(StateError, "nie oczekuje"):
            set_sort_date(self.config, import_id + 1, date(2025, 2, 1))
        self._cli("sort-date", str(import_id), "2025-02-01")
        planned, = plan_sort(self.config)
        self.assertEqual(planned.state, "ready")
        self.assertEqual(planned.manual_date, date(2025, 2, 1))
        self.assertTrue(planned.target.as_posix().endswith("/2025/02.2025/photo.png"))
        self.assertEqual(staging.read_bytes(), _photo())
        self.assertFalse(self.config.local.undated_root.exists())

    def test_missing_or_changed_staging_never_reaches_exiftool(self) -> None:
        _, staging = self._import(Path("/camera/photo.jpg"), b"expected")
        staging.write_bytes(b"replaced")
        with patch("media_archive.sort_planning.read_metadata", side_effect=AssertionError):
            changed, = plan_sort(self.config)
            self.assertEqual(changed.state, "blocked")
            self.assertIn("SHA-256", changed.problem)
            staging.unlink()
            missing, = plan_sort(self.config)
            self.assertEqual(missing.state, "blocked")
            self.assertIn("--reimport", missing.problem)

    @unittest.skipUnless(shutil.which("exiftool"), "requires ExifTool")
    def test_preview_rejects_a_symlinked_sort_destination(self) -> None:
        import_id, _ = self._import(Path("/camera/photo.png"), _photo())
        collection = self.config.collection("photos_main").local_root
        collection.mkdir(parents=True)
        self.config.local.undated_root.symlink_to(collection, target_is_directory=True)
        undated, = plan_sort(self.config)
        self.assertEqual(undated.state, "blocked")
        self.assertIsNone(undated.target)
        self.assertIn("dowiązanie", undated.problem)

        other = self.config.collection("photos_secondary").local_root
        other.mkdir(parents=True)
        (collection / "2025").symlink_to(other, target_is_directory=True)
        set_sort_date(self.config, import_id, date(2025, 2, 1))
        dated, = plan_sort(self.config)
        self.assertEqual(dated.state, "blocked")
        self.assertIsNone(dated.target)
        self.assertIn("dowiązanie", dated.problem)

    @unittest.skipUnless(shutil.which("exiftool"), "requires ExifTool")
    def test_video_timestamp_uses_quicktime_epoch_in_a_non_utc_host_timezone(self) -> None:
        path = self.root / "sample.mp4"
        path.write_bytes(_movie(datetime(1969, 12, 31, 23, 59, 59, tzinfo=timezone.utc)))
        with patch.dict(os.environ, TZ="America/Los_Angeles"), path.open("rb") as stream:
            metadata = read_metadata(stream, path, ZoneInfo("Europe/Warsaw"))
        self.assertEqual(metadata.raw_date, "1969:12:31 23:59:59+00:00")
        self.assertEqual(metadata.timestamp.isoformat(), "1970-01-01T00:59:59+01:00")

    def test_tool_failure_and_invalid_dates_are_distinct(self) -> None:
        path = self.root / "test.jpg"
        path.touch()
        zone = ZoneInfo("Europe/Warsaw")
        with path.open("rb") as stream:
            with patch("media_archive.metadata.subprocess.run", side_effect=FileNotFoundError):
                with self.assertRaises(MetadataToolError):
                    read_metadata(stream, path, zone)
            result = subprocess.CompletedProcess([], 0, b"""[{
                "ExifTool:ExifToolVersion":13.50,"ExifIFD:DateTimeOriginal":"2025:02:29 10:00:00"
            }]""")
            with patch("media_archive.metadata.subprocess.run", return_value=result):
                invalid = read_metadata(stream, path, zone)
        self.assertIsNone(invalid.timestamp)
        self.assertEqual(invalid.raw_date, "2025:02:29 10:00:00")
        self.assertIn("Nieprawidłowa", invalid.diagnostics[0])

    @unittest.skipUnless(shutil.which("exiftool"), "requires ExifTool")
    def test_video_timestamp_keeps_the_correct_offset_across_fall_transition(self) -> None:
        zone = ZoneInfo("Europe/Warsaw")
        path = self.root / "sample.mp4"
        for hour, offset in ((0, "+02:00"), (1, "+01:00")):
            path.write_bytes(_movie(datetime(2025, 10, 26, hour, 30, tzinfo=timezone.utc)))
            with path.open("rb") as stream:
                metadata = read_metadata(stream, path, zone)
            self.assertEqual(metadata.timestamp.isoformat(), f"2025-10-26T02:30:00{offset}")

    @unittest.skipUnless(shutil.which("exiftool"), "requires ExifTool")
    def test_sort_moves_undated_import_and_places_it_after_a_manual_date(self) -> None:
        import_id, staging = self._import(Path("/camera/photo.png"), _photo())
        inode = staging.stat().st_ino
        first, = execute_sort(self.config)
        self.assertEqual(first.status, "undated")
        undated = first.item.target
        self.assertEqual(undated.read_bytes(), _photo())
        self.assertEqual(undated.stat().st_ino, inode)
        self.assertFalse(staging.exists())
        repeated, = execute_sort(self.config)
        self.assertIsNone(repeated.item.operation_id)
        set_sort_date(self.config, import_id, date(2025, 2, 1))
        final, = execute_sort(self.config)
        self.assertEqual(final.status, "placed")
        self.assertFalse(undated.exists())
        self.assertEqual(final.item.target.read_bytes(), _photo())
        self.assertEqual(execute_sort(self.config), ())
        new_import, staging = self._import(Path("/camera/photo.png"), _photo())
        set_sort_date(self.config, new_import, date(2025, 2, 1))
        with patch.object(Part, "create", side_effect=AssertionError("identical destination should be reused")):
            repeated, = execute_sort(self.config)
        self.assertEqual(repeated.status, "placed")
        self.assertEqual(repeated.item.target, final.item.target)
        self.assertFalse(staging.exists())
        with open_database(self.database) as connection:
            self.assertIsNotNone(history.load_placement(connection, import_id))
            self.assertIsNotNone(history.load_placement(connection, new_import))
            self.assertTrue(all(move.cleanup_done for move in sort_moves.load_moves(connection)))

    @unittest.skipUnless(shutil.which("exiftool") and shutil.which("rsync"), "requires ExifTool and rsync")
    def test_sort_keeps_unicode_name_conflicts_and_extends_a_hash_suffix(self) -> None:
        sample = _movie(datetime(2025, 2, 5, tzinfo=timezone.utc))
        _, staging = self._import(Path("/camera/Łódź.mp4"), sample)
        plan, = plan_sort(self.config)
        base = plan.target
        base.parent.mkdir(parents=True)
        occupied = base.with_name("ŁÓDŹ.MP4")
        occupied.write_bytes(b"first existing variant")
        prefix = sha256(sample).hexdigest()
        short = base.with_name(f"{base.stem}-{prefix[:8]}{base.suffix}")
        short.write_bytes(b"second existing variant")
        with patch("media_archive._transfer_files.os.link", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")):
            self._cli("sort")
        written = base.with_name(f"{base.stem}-{prefix[:12]}{base.suffix}")
        self.assertEqual(written.read_bytes(), sample)
        self.assertEqual(occupied.read_bytes(), b"first existing variant")
        self.assertEqual(short.read_bytes(), b"second existing variant")
        self.assertFalse(staging.exists())
        with open_database(self.database) as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM sort_conflicts").scalar_one(), 1)
            self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM sort_conflict_members").scalar_one(), 3)

    @unittest.skipUnless(shutil.which("exiftool"), "requires ExifTool")
    def test_resume_rechecks_publication_and_preserves_working_copy_when_target_changes(self) -> None:
        sample = _movie(datetime(2025, 2, 5, tzinfo=timezone.utc))
        import_id, staging = self._import(Path("/camera/movie.mp4"), sample)
        publish = Destination.publish_link

        def after_publication(destination, *args):
            publish(destination, *args)
            raise KeyboardInterrupt

        with patch.object(Destination, "publish_link", after_publication):
            interrupted, = execute_sort(self.config)
        self.assertEqual(interrupted.status, "interrupted")
        self.assertTrue(staging.exists())
        operation = interrupted.item.operation_id
        target = interrupted.item.target
        self.assertEqual(target.read_bytes(), sample)
        with open_database(self.database) as connection:
            self.assertIsNone(history.load_placement(connection, import_id))
        with self.assertRaisesRegex(StateError, f"resume {operation}"):
            set_sort_date(self.config, import_id, date(2025, 2, 6))
        with patch("media_archive.sort_execution._cleanup", side_effect=KeyboardInterrupt):
            self.assertEqual(resume_sort(self.config, operation).status, "interrupted")
        self.assertIn("oczekuje na usunięcie", self._cli("pending"))
        saved = self.root / "published"
        target.rename(saved)
        target.write_bytes(b"unrelated")
        self.assertEqual(resume_sort(self.config, operation).status, "blocked")
        self.assertEqual(staging.read_bytes(), sample)
        self.assertEqual(target.read_bytes(), b"unrelated")
        saved.replace(target)
        self._cli("resume", str(operation))
        self.assertFalse(staging.exists())
        self.assertEqual(target.read_bytes(), sample)
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertEqual(list(target.parent.glob(".media-archive-transfer-*")), [])

    @unittest.skipUnless(shutil.which("exiftool"), "requires ExifTool")
    def test_restore_reconciles_a_sort_published_after_the_snapshot(self) -> None:
        import_id, staging = self._import(Path("/camera/movie.mp4"),
                                         _movie(datetime(2025, 2, 5, tzinfo=timezone.utc)))
        with patch.object(Destination, "publish_link", side_effect=KeyboardInterrupt):
            interrupted, = execute_sort(self.config)
        operation = interrupted.item.operation_id
        generations = self.root / "generations"
        generations.mkdir()
        snapshot = create_snapshot(self.config, generations)
        self.assertEqual(resume_sort(self.config, operation).status, "placed")
        self.assertFalse(staging.exists())
        restore_snapshot(self.config, snapshot)
        self.assertEqual(reconcile_imports(self.config)["Sprawdzone operacje SORT"], 1)
        with open_database(self.database) as connection:
            self.assertIsNone(load_reconciliation(connection))
            self.assertIsNotNone(history.load_placement(connection, import_id))
        self.assertEqual(resume_sort(self.config, operation).status, "placed")

    @unittest.skipUnless(shutil.which("exiftool"), "requires ExifTool")
    def test_sort_resume_blocks_new_alias_and_leaves_no_linked_part_before_publication(self) -> None:
        sample = _movie(datetime(2025, 2, 5, tzinfo=timezone.utc))
        _, staging = self._import(Path("/camera/movie.mp4"), sample)
        with patch("media_archive._transfer_files.os.link", side_effect=KeyboardInterrupt):
            interrupted, = execute_sort(self.config)
        self.assertEqual(interrupted.status, "interrupted")
        target = interrupted.item.target
        self.assertFalse(target.exists())
        self.assertEqual(staging.stat().st_nlink, 1)
        self.assertEqual(list(target.parent.glob(".media-archive-transfer-*")), [])
        other = target.with_name("MOVIE.MP4")
        other.write_bytes(b"external file")
        blocked = resume_sort(self.config, interrupted.item.operation_id)
        self.assertEqual(blocked.status, "blocked")
        self.assertIn("pisownią", blocked.item.problem)
        self.assertFalse(target.exists())
        self.assertEqual(other.read_bytes(), b"external file")
        self.assertEqual(staging.read_bytes(), sample)
        other.rename(self.root / "external")
        resumed = resume_sort(self.config, interrupted.item.operation_id)
        self.assertEqual(resumed.status, "placed")
        self.assertFalse(staging.exists())
        self.assertEqual(target.stat().st_nlink, 1)

    @unittest.skipUnless(shutil.which("exiftool"), "requires ExifTool")
    def test_restore_reports_unknown_undated_copy_and_keeps_block(self) -> None:
        _, staging = self._import(Path("/camera/photo.png"), _photo())
        generations = self.root / "generations"
        generations.mkdir()
        snapshot = create_snapshot(self.config, generations)
        moved, = execute_sort(self.config)
        self.assertEqual(moved.status, "undated")
        self.assertFalse(staging.exists())
        restore_snapshot(self.config, snapshot)
        with self.assertRaisesRegex(StateError, "Do wyjaśnienia zawiera nierozpoznany wpis"):
            reconcile_imports(self.config)
        with open_database(self.database) as connection:
            self.assertIsNotNone(load_reconciliation(connection))
        self.assertEqual(moved.item.target.read_bytes(), _photo())

    @unittest.skipUnless(shutil.which("exiftool"), "requires ExifTool")
    def test_catalog_keeps_conflict_history_and_verifies_manual_moves(self) -> None:
        sample = _movie(datetime(2025, 2, 5, tzinfo=timezone.utc))
        import_id, _ = self._import(Path("/camera/clip.mp4"), sample)
        plan, = plan_sort(self.config)
        existing = plan.target
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"older variant")
        result, = execute_sort(self.config)
        placed = result.item.target
        self.assertNotEqual(placed, existing)
        group, = list_conflicts(self.config)
        self.assertEqual(len(group["warianty"]), 2)
        self.assertIn("do przejrzenia", self._cli("conflicts"))
        self._cli("conflicts", "--keep-both", group["grupa"])
        self.assertEqual(placed.read_bytes(), sample)
        self.assertEqual(existing.read_bytes(), b"older variant")
        self.assertIn("zachowaj oba", self._cli("conflicts"))

        known = next(item for item in group["warianty"] if Path(item["ścieżka"]) == placed)
        target = self.config.collection("photos_main").local_root / "renamed.mp4"
        target.write_bytes(sample)
        self.assertIn("nadal znajduje się",
                      self._cli("catalog-move", str(known["id_lokalizacji"]), str(target), expected=1))
        placed.unlink()
        target.write_bytes(b"x" * len(sample))
        before = self.database.read_bytes()
        self.assertIn("SHA-256", self._cli("catalog-move", str(known["id_lokalizacji"]), str(target), expected=1))
        self.assertEqual(self.database.read_bytes(), before)
        target.write_bytes(sample)
        self._cli("catalog-move", str(known["id_lokalizacji"]), str(target))
        updated, = search_catalog(self.config, name="renamed", source=("phone", "main"), month="2025-02")
        self.assertEqual(updated["ścieżka"], str(target))
        self.assertEqual(target.read_bytes(), sample)
        with open_database(self.database) as connection:
            placement = history.load_placement(connection, import_id)
            self.assertEqual(placement.local_occurrence_id, known["id_lokalizacji"])

        existing.unlink()
        reviewed, = list_conflicts(self.config)
        self.assertIsNotNone(reviewed["zachowaj_oba_od"])
        self.assertEqual(len(reviewed["warianty"]), 2)
        moved = next(item for item in reviewed["warianty"] if Path(item["ścieżka"]) == placed)
        self.assertEqual(moved["przeniesiono_do"], str(target))

    @unittest.skipUnless(shutil.which("exiftool"), "requires ExifTool")
    def test_catalog_move_links_every_import_that_reused_the_same_copy(self) -> None:
        sample = _movie(datetime(2025, 2, 5, tzinfo=timezone.utc))
        first, _ = self._import(Path("/camera/clip.mp4"), sample)
        placed, = execute_sort(self.config)
        second, _ = self._import(Path("/camera/clip.mp4"), sample)
        reused, = execute_sort(self.config)
        self.assertEqual(reused.item.target, placed.item.target)
        self._import(Path("/camera/other.mp4"), sample)
        other, = execute_sort(self.config)
        with open_database(self.database) as connection:
            old_ids = [history.load_placement(connection, item).local_occurrence_id for item in (first, second)]
        self.assertNotEqual(*old_ids)

        destination = placed.item.target.with_name("renamed.mp4")
        placed.item.target.rename(destination)
        self._cli("catalog-move", str(old_ids[0]), str(destination))

        rows = search_catalog(self.config)
        current, = (item for item in rows if item["ścieżka"] == str(destination))
        previous = {item["id"]: item for item in rows if item["ścieżka"] == str(placed.item.target)}
        self.assertEqual(set(previous), set(old_ids))
        for item in previous.values():
            self.assertEqual(item["obecność"], "przeniesiono")
            self.assertEqual(item["następna_lokalizacja"], current["id"])
        separate, = (item for item in rows if item["ścieżka"] == str(other.item.target))
        self.assertIsNone(separate["następna_lokalizacja"])
        self.assertEqual(other.item.target.read_bytes(), sample)
        self.assertEqual(destination.read_bytes(), sample)
        with open_database(self.database) as connection:
            self.assertEqual([history.load_placement(connection, item).local_occurrence_id
                              for item in (first, second)], old_ids)
