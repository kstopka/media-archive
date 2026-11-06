"""Reusable Pydantic field types and validators for individual values."""

from pathlib import Path, PurePosixPath
import re
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BeforeValidator, Field, StringConstraints, TypeAdapter


def _absolute_path(path: Path) -> Path:
    # Linux treats the POSIX double-slash anchor as the same filesystem root.
    if path.anchor == "//":
        path = Path("/") / path.relative_to("//")
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise ValueError("Wymagana bezwzględna ścieżka bez przejść do rodzica.")
    return path


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in value:
        raise ValueError("Wymagana względna ścieżka bez przejść do rodzica.")
    return str(path)


def _single_folder(value: str) -> str:
    value = _relative_path(value)
    if len(PurePosixPath(value).parts) != 1:
        raise ValueError("Wymagana nazwa pojedynczego katalogu.")
    return value


def _not_placeholder(value: str) -> str:
    if value.startswith("REPLACE_WITH_"):
        raise ValueError("Należy uzupełnić przykładowy identyfikator.")
    return value


def _decimal(value: object) -> object:
    # Keep integers in .env unambiguous: no fractions, exponents or booleans.
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        return value
    if type(value) is int:
        return value
    raise ValueError("Wymagana liczba całkowita w zapisie dziesiętnym.")


def _timezone(value: object) -> ZoneInfo:
    if isinstance(value, ZoneInfo):
        return value
    try:
        if not isinstance(value, str):
            raise TypeError
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("Niepoprawna lub niedostępna strefa czasowa.") from None


def _extensions(value: object) -> object:
    if isinstance(value, str):
        return tuple(part.strip().upper() for part in value.split(",")) if value.strip() else ()
    return value


NonEmpty = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Identifier = Annotated[
    str, StringConstraints(min_length=1, pattern=r"^[^\s/\\]+$"),
    AfterValidator(_not_placeholder),
]
ConfigFilePath = Annotated[Path, BeforeValidator(TypeAdapter(NonEmpty).validate_python)]
AbsolutePath = Annotated[Path, AfterValidator(_absolute_path)]
RelativePath = Annotated[NonEmpty, AfterValidator(_relative_path)]
SingleFolder = Annotated[NonEmpty, AfterValidator(_single_folder)]
PositiveCount = Annotated[int, BeforeValidator(_decimal), Field(gt=0)]
ReserveBytes = Annotated[int, BeforeValidator(_decimal), Field(ge=1024**3)]
Timezone = Annotated[ZoneInfo, BeforeValidator(_timezone)]
Extension = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]+$")]
IgnoredExtensions = Annotated[tuple[Extension, ...], BeforeValidator(_extensions)]

RegistryId = Annotated[
    str, BeforeValidator(lambda value: value.lower() if isinstance(value, str) else value),
    StringConstraints(pattern=r"^[a-z][a-z0-9_]*$"),
]
