"""Shared behavior for deeply composable domain models."""

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    """Reject unknown fields and prevent mutation after validation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )
