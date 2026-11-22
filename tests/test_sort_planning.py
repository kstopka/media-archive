"""Exercise SORT previews with verified staging and synthetic media."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
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
from media_archive.config import load_config
from media_archive.devices import registered_volume
from media_archive.metadata import MetadataToolError, read_metadata
from media_archive.sort_planning import plan_sort, set_sort_date
from media_archive.state import DeviceKey, StateError, history, open_database, transaction
from media_archive.state.registry import initialize_database
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
        self.enterContext(patch("media_archive.sort_planning.verify_volume",
                                side_effect=lambda *_: mounted(self.volume.root)))

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
        self.assertTrue(by_name["photo.png"]["cel"].endswith("/2024/2024.12/photo.png"))
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
        self.assertTrue(planned.target.as_posix().endswith("/2025/2025.02/photo.png"))
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
