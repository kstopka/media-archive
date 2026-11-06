"""Read one file, validate its configuration without writing state."""

from pathlib import Path

from .env import parse_env
from .errors import ConfigError
from .models import Configuration
from .registry import load_registry
from .validation import validate_relationships


def load_config(path: str | Path = ".env") -> Configuration:
    source = Path(path).absolute()
    try:
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError, ValueError):
        raise ConfigError("Nie można odczytać pliku konfiguracji jako UTF-8.") from None
    values = parse_env(text)
    config = load_registry(values, source)
    validate_relationships(config)
    return config
