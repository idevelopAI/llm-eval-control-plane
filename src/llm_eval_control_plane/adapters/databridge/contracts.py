"""Strict DataBridge request, wire-response, and mock-fixture contracts."""

from __future__ import annotations

from typing import Annotated, Literal, Self, TypeAlias

from pydantic import (
    Field,
    FiniteFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from llm_eval_control_plane.domain.canonical import canonical_json_bytes
from llm_eval_control_plane.domain.models import FrozenModel

DataBridgeLanguage: TypeAlias = Literal["en", "de"]


class DataBridgeInput(FrozenModel):
    """The complete, expectation-free input sent to DataBridge."""

    question: Annotated[StrictStr, Field(min_length=1, max_length=1_000)]
    chat_history: Annotated[StrictStr, Field(max_length=4_000)] = ""
    language: DataBridgeLanguage = "de"


class DataBridgeExecution(FrozenModel):
    """One SQL execution in a successful DataBridge wire response."""

    sql: Annotated[StrictStr, Field(min_length=1, max_length=32_768)]
    columns: Annotated[tuple[StrictStr, ...], Field(max_length=1_024)]
    rows: Annotated[tuple[tuple[object, ...], ...], Field(max_length=10_001)]
    row_count: Annotated[StrictInt, Field(ge=0)]
    truncated: bool
    duration_ms: Annotated[FiniteFloat, Field(ge=0)]

    @field_validator("rows")
    @classmethod
    def require_json_rows(
        cls, value: tuple[tuple[object, ...], ...]
    ) -> tuple[tuple[object, ...], ...]:
        for row in value:
            canonical_json_bytes(list(row))
        return value


class DataBridgeQueryResponse(FrozenModel):
    """DataBridge v1.2.0 ``POST /api/v1/query`` success response."""

    status: Literal["answered", "clarification_required"]
    answer: Annotated[StrictStr, Field(max_length=262_144)]
    executions: Annotated[tuple[DataBridgeExecution, ...], Field(max_length=16)]
    duration_ms: Annotated[FiniteFloat, Field(ge=0)]
    request_id: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    model_duration_ms: Annotated[FiniteFloat, Field(ge=0)]
    tool_call_count: Annotated[StrictInt, Field(ge=0)]
    input_tokens: Annotated[StrictInt, Field(ge=0)]
    output_tokens: Annotated[StrictInt, Field(ge=0)]

    @model_validator(mode="after")
    def require_status_shape(self) -> Self:
        if self.status == "answered" and not self.executions:
            raise ValueError("answered responses require a SQL execution")
        if self.status == "clarification_required" and self.executions:
            raise ValueError("clarification responses cannot contain SQL executions")
        return self


class DataBridgeMockSuccess(FrozenModel):
    """One offline fixture carrying a real success-body shape."""

    status: Literal[200]
    body: DataBridgeQueryResponse


class DataBridgeMockRefusal(FrozenModel):
    """One offline fixture representing a policy rejection."""

    status: Literal[403]


DataBridgeMockFixture: TypeAlias = Annotated[
    DataBridgeMockSuccess | DataBridgeMockRefusal,
    Field(discriminator="status"),
]
