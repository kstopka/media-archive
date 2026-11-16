"""Source formats shared by phone initialization and import planning."""

from pathlib import Path

PHONE_MEDIA = frozenset(("jpg", "jpeg", "png", "heic", "heif", "mp4", "mov"))
CAMERA_MEDIA = frozenset(("jpg", "jpeg", "mp4"))


def media_extension(path: Path) -> str:
    return path.suffix.removeprefix(".").casefold()
