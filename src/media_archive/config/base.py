"""Shared immutable Pydantic model with private representations."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from media_archive._encoding import require_utf8


class ConfigModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True,
        validate_default=True, arbitrary_types_allowed=True,
    )

    @field_validator("*", mode="after")
    @classmethod
    def _validate_utf8(cls, value: object) -> object:
        if isinstance(value, (str, Path)):
            require_utf8(str(value))
        return value

    def __repr_args__(self):
        return ()

    def __str__(self) -> str:
        return repr(self)
