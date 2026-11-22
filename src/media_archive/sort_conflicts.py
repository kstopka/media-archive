"""Choose a non-overwriting name on a case-insensitive, Unicode-aware path."""

from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

from media_archive._encoding import require_utf8
from media_archive._transfer_files import Destination, TransferConflict
from media_archive.sources import SourceDigest
from media_archive.state import StateError

_RESERVED = re.compile(r"CON|PRN|AUX|NUL|(?:COM|LPT)[1-9¹²³]", re.IGNORECASE)
_INVALID = frozenset('<>:"/\\|?*')


def _name_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).rstrip(" .").casefold()


def _validate_name(value: str) -> None:
    require_utf8(value, StateError)
    if (value != value.rstrip(" .") or any(char in _INVALID or ord(char) < 32 for char in value)
            or _RESERVED.fullmatch(value.split(".")[0].rstrip(" "))
            or len(value.encode("utf-8")) > 255 or len(value.encode("utf-16-le")) > 510):
        raise StateError("Nazwa pliku SORT nie jest obsługiwana na docelowym systemie plików.")


@dataclass(frozen=True)
class NameChoice:
    path: Path
    group_path: str | None
    variants: tuple[tuple[Path, SourceDigest], ...]


def require_destination_name(destination: Destination) -> None:
    """Reject a name that has acquired a new normalized alias since planning."""
    name = destination.path.name
    matches = [entry for entry in destination.sibling_names(_name_key) if _name_key(entry) == _name_key(name)]
    if matches and matches != [name]:
        raise TransferConflict("Zapisany cel SORT koliduje z inną pisownią nazwy w katalogu.")


def choose_name(destination: Destination, expected: SourceDigest) -> NameChoice:
    """Reuse equal content, or extend the digest suffix until a name is free."""
    path = destination.path
    _validate_name(path.name)
    siblings: dict[str, list[str]] = {}
    for name in destination.sibling_names(_name_key):
        siblings.setdefault(_name_key(name), []).append(name)
    variants = []
    for length in (0, *range(8, 65, 4)):
        name = f"{path.stem}-{expected.sha256[:length]}{path.suffix}" if length else path.name
        _validate_name(name)
        matches = siblings.get(_name_key(name), ())
        group = str(destination.volume.root / path.parent / _name_key(path.name)) if variants else None
        if not matches:
            return NameChoice(path.with_name(name), group, tuple(variants))
        if len(matches) != 1:
            raise StateError("Katalog zawiera niejednoznaczne nazwy kolidujące po normalizacji.")
        actual = path.with_name(matches[0])
        digest = Destination(destination.volume, actual).recover_digest(sync=False)
        if digest is None:
            raise StateError("Zawartość katalogu zmieniła się podczas sprawdzania kolizji.")
        if digest == expected:
            return NameChoice(actual, group, tuple(variants))
        variants.append((actual, digest))
    raise StateError("Brak bezpiecznej nazwy nawet z pełnym skrótem SHA-256.")
