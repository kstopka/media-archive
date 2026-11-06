"""Read .env syntax with python-dotenv and check parameter names."""

from io import StringIO
import re

from dotenv.parser import parse_stream

from .errors import ConfigError


_KEY_GROUPS = {
    "APP": "TIMEZONE CONFIG_FILE DATABASE_FILE REPORTS_DIR MIN_FREE_BYTES "
    "REPORT_KEEP_RUNS DATABASE_KEEP_GENERATIONS ARCHIVE_STATE_DIR",
    "LOCAL": "VOLUMES STAGING_ROOT UNDATED_ROOT",
    "CAMERA": "PROFILES CARDS",
}


KNOWN_KEYS = {"PHONE_PROFILES", "POLICIES", "COLLECTIONS", "ARCHIVE_GROUPS", "BACKUP_PROFILES"} | {
    f"{prefix}_{suffix}"
    for prefix, suffixes in _KEY_GROUPS.items()
    for suffix in suffixes.split()
}


# Named entries use the same schema regardless of their name or number.
PHONE_KEY = re.compile(
    r"PHONE_([A-Z][A-Z0-9_]*?)_(ALIAS|LABEL|MODEL|ANDROID_VERSION|BUILD|TRANSPORT|SERIAL|CAMERA_DIR|INITIAL_POLICY|COLLECTION)$"
)
CAMERA_KEY = re.compile(
    r"CAMERA_([A-Z][A-Z0-9_]*?)_(ALIAS|LABEL|MODEL|CAMERA_FOLDER|SOURCE_RELATIVE|SOURCE_EXAMPLE_SUBDIR|IGNORE_EXTENSIONS|CARDS|COLLECTION)$"
)
CAMERA_CARD_KEY = re.compile(r"CAMERA_CARD_([A-Z][A-Z0-9_]*?)_UUID$")
VOLUME_KEY = re.compile(r"LOCAL_VOLUME_([A-Z][A-Z0-9_]*?)_(ROOT|UUID)$")


POLICY_KEY = re.compile(r"POLICY_([A-Z][A-Z0-9_]*?)_(CHANGES|DELETIONS)$")
COLLECTION_KEY = re.compile(
    r"COLLECTION_([A-Z][A-Z0-9_]*?)_(LABEL|LOCAL_ROOT|ARCHIVE_RELATIVE|POLICY|PARENT|LAYOUT|ALLOW_DELETE)$"
)
ARCHIVE_GROUP_KEY = re.compile(r"ARCHIVE_([A-Z][A-Z0-9_]*?)_(ROOT_RELATIVE|TARGETS)$")
# Syntax only: captures cannot resolve ownership when names contain _TARGET_.
# registry.py assigns target keys using known groups and explicit target lists.
ARCHIVE_TARGET_KEY = re.compile(r"ARCHIVE_([A-Z][A-Z0-9_]*?)_TARGET_([A-Z][A-Z0-9_]*?)_(MOUNT|UUID|STATE)$")
BACKUP_KEY = re.compile(r"BACKUP_([A-Z][A-Z0-9_]*?)_(LABEL|ARCHIVE_GROUP|COLLECTIONS|MONTH_FILTER)$")


def is_known_key(key: str) -> bool:
    return key in KNOWN_KEYS or any(pattern.fullmatch(key) for pattern in (PHONE_KEY, CAMERA_KEY, CAMERA_CARD_KEY, VOLUME_KEY, POLICY_KEY, COLLECTION_KEY, ARCHIVE_GROUP_KEY, ARCHIVE_TARGET_KEY, BACKUP_KEY))


def entry_names(values: dict[str, str], key: str, inferred: set[str]) -> tuple[str, ...]:
    if key not in values:
        return tuple(sorted(inferred))
    raw = values[key]
    names = tuple(name.strip().upper() for name in raw.split(",")) if raw.strip() else ()
    if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) for name in names):
        raise ConfigError(f"Niepoprawna lista identyfikatorów {key}.")
    if len(set(names)) != len(names):
        raise ConfigError(f"Powtórzony identyfikator w {key}.")
    if inferred - set(names):
        raise ConfigError(f"Ustawienia wpisu spoza listy {key}.")
    return names


def parse_env(text: str) -> dict[str, str]:
    """Use python-dotenv syntax, then validate application keys and duplicates."""
    values: dict[str, str] = {}
    # The library parser preserves duplicate assignments and syntax errors.
    # Unlike dotenv_values(), it does not silently skip malformed statements
    # or collapse duplicate keys. It never performs variable interpolation.
    for binding in parse_stream(StringIO(text)):
        number = binding.original.line
        if binding.error:
            raise ConfigError(f"Niepoprawne przypisanie w konfiguracji (wiersz {number}).")
        key, value = binding.key, binding.value
        if key is None:
            continue
        if not is_known_key(key):
            raise ConfigError(f"Nieznany parametr konfiguracji (wiersz {number}).")
        if key in values:
            raise ConfigError(f"Powtórzony parametr {key} (wiersz {number}).")
        if value is None:
            raise ConfigError(f"Brak przypisania wartości {key} (wiersz {number}).")
        if any(ord(char) < 32 and char != "\t" for char in value):
            raise ConfigError(f"Niedozwolony znak w {key} (wiersz {number}).")
        values[key] = value
    return values
