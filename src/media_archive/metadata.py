"""Read only the embedded metadata used by SORT."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from typing import BinaryIO
from zoneinfo import ZoneInfo

from media_archive._media_formats import media_extension
from media_archive.state import StateError

_TIMESTAMP = re.compile(r"[0-9]{4}:[0-9]{2}:[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")


class MetadataToolError(StateError):
    """ExifTool could not provide a usable response."""


@dataclass(frozen=True)
class MediaMetadata:
    tag: str
    raw_date: str | None
    timestamp: datetime | None
    tool_version: str
    diagnostics: tuple[str, ...]


def read_metadata(stream: BinaryIO, name: Path, zone: ZoneInfo) -> MediaMetadata:
    """Read a pinned file without writing tags or applying the host timezone."""
    video = media_extension(name) in ("mp4", "mov")
    group = "QuickTime" if video else "ExifIFD"
    tag = "CreateDate" if video else "DateTimeOriginal"
    query = f"-{group}:{tag}"
    descriptor = stream.fileno()
    try:
        result = subprocess.run(
            ["exiftool", "-config", "", "-j", "-G1", "-api", "QuickTimeUTC=1", query,
             "-Error", "-Warning", "-ExifToolVersion", f"/proc/self/fd/{descriptor}"],
            env={**os.environ, "TZ": "UTC"},
            pass_fds=(descriptor,), stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False,
        )
    except FileNotFoundError:
        raise MetadataToolError("Nie znaleziono ExifTool. Jest wymagany do odczytu metadanych SORT.") from None
    except (OSError, subprocess.TimeoutExpired):
        raise MetadataToolError("Nie można uruchomić lub zakończyć odczytu ExifTool.") from None
    try:
        rows = json.loads(result.stdout.decode("utf-8"), parse_float=str)
    except (UnicodeDecodeError, ValueError):
        raise MetadataToolError("ExifTool nie zwrócił poprawnego wyniku JSON.") from None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise MetadataToolError("ExifTool nie zwrócił metadanych jednego pliku.")
    row = rows[0]
    version = row.get("ExifTool:ExifToolVersion")
    if not isinstance(version, (str, int, float)):
        raise MetadataToolError("ExifTool nie zwrócił wersji narzędzia.")
    diagnostics = tuple(row[key] for key in ("ExifTool:Warning", "ExifTool:Error") if isinstance(row.get(key), str))
    qualified = f"{group}:{tag}"
    raw_date = row.get(qualified)
    raw = raw_date if isinstance(raw_date, str) else None
    if result.returncode and "ExifTool:Error" not in row:
        raise MetadataToolError("ExifTool zakończył się błędem bez wyniku odczytu pliku.")
    if "ExifTool:Error" in row:
        return MediaMetadata(qualified, raw, None, str(version), diagnostics)
    if raw is None:
        return MediaMetadata(qualified, raw, None, str(version), (*diagnostics, "Brak daty w metadanych."))
    try:
        # ExifTool uses a UTC process timezone for QuickTime's 1904 epoch conversion.
        value = raw.removesuffix("+00:00") if video else raw
        if _TIMESTAMP.fullmatch(value) is None:
            raise ValueError
        timestamp = datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
        if video:
            timestamp = timestamp.replace(tzinfo=timezone.utc).astimezone(zone)
    except (ValueError, OverflowError):
        return MediaMetadata(qualified, raw, None, str(version), (*diagnostics, "Nieprawidłowa data w metadanych."))
    return MediaMetadata(qualified, raw, timestamp, str(version), diagnostics)
