"""Shared immutable Pydantic model with private representations."""

from pydantic import BaseModel, ConfigDict


class ConfigModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True,
        validate_default=True, arbitrary_types_allowed=True,
    )

    def __repr_args__(self):
        return ()

    def __str__(self) -> str:
        return repr(self)
