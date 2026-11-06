"""Build DTOs without exposing private values in validation errors."""

from collections.abc import Iterable, Mapping
from typing import TypeVar

from pydantic import ValidationError

from .base import ConfigModel
from .env import is_known_key
from .errors import ConfigError

Model = TypeVar("Model", bound=ConfigModel)


def build(model: type[Model], data: Mapping[str, object], prefix: str = "") -> Model:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        keys = set()
        for item in error.errors(include_input=False, include_context=False, include_url=False):
            location = item["loc"]
            field = str(location[0]) if location else ""
            env_field = {
                "policy_id": "policy", "parent_id": "parent", "collection_id": "collection",
                "archive_group_id": "archive_group", "collection_ids": "collections",
            }.get(field, field)
            candidate = f"{prefix}_{env_field.upper()}" if location else prefix
            keys.add(candidate if is_known_key(candidate) else (prefix or "konfiguracja"))
        raise ConfigError("Niepoprawne lub brakujące ustawienia: " + ", ".join(sorted(keys)) + ".") from None


def fields(values: dict[str, str], prefix: str, names: Iterable[str]) -> dict[str, str]:
    """Read exact fields, even when registry names share prefixes."""
    return {name: values[f"{prefix}_{name.upper()}"] for name in names
            if f"{prefix}_{name.upper()}" in values}
