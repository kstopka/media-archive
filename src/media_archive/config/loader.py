"""Read one file, validate its configuration without writing state."""

from pathlib import Path

from media_archive._encoding import require_utf8

from .env import parse_env
from .errors import ConfigError
from .models import Configuration
from .registry import load_registry
from .validation import validate_relationships


def load_config(path: str | Path = ".env") -> Configuration:
    source = Path(path).absolute()
    require_utf8(str(source), ConfigError)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError, ValueError):
        raise ConfigError("Nie można odczytać pliku konfiguracji jako UTF-8.") from None
    return _parse_config(source, text)


def _parse_config(source: Path, text: str) -> Configuration:
    values = parse_env(text)
    config = load_registry(values, source)
    validate_relationships(config)
    return config
