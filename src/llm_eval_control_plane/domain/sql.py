"""Strict canonical contracts for DataBridge SQL evaluation evidence."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self, TypeAlias

from pydantic import (
    ConfigDict,
    Field,
    FiniteFloat,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from llm_eval_control_plane.domain.canonical import CanonicalJson
from llm_eval_control_plane.domain.execution import SafeCode
from llm_eval_control_plane.domain.models import FrozenModel

SqlText = Annotated[StrictStr, Field(min_length=1, max_length=32_768)]
SqlColumn = Annotated[StrictStr, Field(min_length=1, max_length=256)]
SqlScalar: TypeAlias = StrictBool | StrictInt | FiniteFloat | StrictStr | None
SqlRow: TypeAlias = tuple[SqlScalar, ...]


class SqlBehavior(StrEnum):
    """Reviewed interaction category used to decide which metrics apply."""

    QUERY = "query"
    CLARIFICATION = "clarification"
    REFUSAL = "refusal"


class SqlResultOrder(StrEnum):
    """Whether result row position is part of the reviewed expectation."""

    ORDERED = "ordered"
    UNORDERED = "unordered"


class _StrictSqlModel(FrozenModel):
    """Forbid coercion at SQL-specific trust boundaries."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class SqlExpectation(_StrictSqlModel):
    """Reviewed SQL oracle stored inside ``EvaluationCase.expected``."""

    schema_version: Literal["1"] = "1"
    behavior: SqlBehavior
    reference_sql: SqlText | None = Field(default=None, repr=False)
    expected_columns: Annotated[tuple[SqlColumn, ...], Field(min_length=1)] | None = (
        None
    )
    expected_rows: tuple[SqlRow, ...] | None = Field(default=None, repr=False)
    result_order: SqlResultOrder | None = None
    accepted_clarification_codes: (
        Annotated[tuple[SafeCode, ...], Field(min_length=1)] | None
    ) = None

    @field_validator("expected_columns")
    @classmethod
    def require_unique_columns(
        cls, value: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("expected column names must be unique")
        return value

    @field_validator("accepted_clarification_codes")
    @classmethod
    def normalize_clarification_codes(
        cls, value: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        if len(value) != len(set(value)):
            raise ValueError("clarification codes must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def require_category_fields(self) -> Self:
        query_field_names = {
            "reference_sql",
            "expected_columns",
            "expected_rows",
            "result_order",
        }
        query_fields = (
            self.reference_sql,
            self.expected_columns,
            self.expected_rows,
            self.result_order,
        )
        if self.behavior is SqlBehavior.QUERY:
            if any(value is None for value in query_fields):
                raise ValueError("query expectations require complete SQL evidence")
            if "accepted_clarification_codes" in self.model_fields_set:
                raise ValueError("query expectations cannot accept clarification codes")
            assert self.expected_columns is not None
            assert self.expected_rows is not None
            if any(
                len(row) != len(self.expected_columns) for row in self.expected_rows
            ):
                raise ValueError("expected row width must match expected columns")
            return self

        if query_field_names & self.model_fields_set:
            raise ValueError("non-query expectations cannot contain SQL evidence")
        if self.behavior is SqlBehavior.CLARIFICATION:
            if self.accepted_clarification_codes is None:
                raise ValueError("clarification expectations require accepted codes")
        elif "accepted_clarification_codes" in self.model_fields_set:
            raise ValueError("refusal expectations cannot accept clarification codes")
        return self

    @classmethod
    def from_canonical(cls, value: CanonicalJson) -> SqlExpectation:
        """Validate a canonical case expectation without permissive coercion."""
        return cls.model_validate_json(value.canonical, strict=True)

    def to_canonical(self) -> CanonicalJson:
        """Return the minimal canonical JSON form used by dataset fixtures."""
        return CanonicalJson.from_value(self.model_dump(mode="json", exclude_none=True))


class SqlTargetOutput(_StrictSqlModel):
    """Minimal target evidence; answers and returned database rows are excluded."""

    schema_version: Literal["1"] = "1"
    kind: SqlBehavior
    sql_executions: (
        Annotated[tuple[SqlText, ...], Field(min_length=1, max_length=16)] | None
    ) = Field(default=None, repr=False)
    clarification_code: SafeCode | None = None

    @model_validator(mode="after")
    def require_category_fields(self) -> Self:
        if self.kind is SqlBehavior.QUERY:
            if self.sql_executions is None:
                raise ValueError("query outputs require SQL executions")
            if "clarification_code" in self.model_fields_set:
                raise ValueError("query outputs cannot include a clarification code")
        elif self.kind is SqlBehavior.CLARIFICATION:
            if self.clarification_code is None:
                raise ValueError("clarification outputs require a stable code")
            if "sql_executions" in self.model_fields_set:
                raise ValueError("clarification outputs cannot include SQL")
        elif {"sql_executions", "clarification_code"} & self.model_fields_set:
            raise ValueError("refusal outputs cannot include category fields")
        return self

    @classmethod
    def from_canonical(cls, value: CanonicalJson) -> SqlTargetOutput:
        """Validate canonical target evidence without permissive coercion."""
        return cls.model_validate_json(value.canonical, strict=True)

    def to_canonical(self) -> CanonicalJson:
        """Return the minimal canonical JSON form persisted by adapters."""
        return CanonicalJson.from_value(self.model_dump(mode="json", exclude_none=True))


class SqlReplayResult(_StrictSqlModel):
    """Bounded, normalized rows returned by a PostgreSQL replay sandbox."""

    columns: tuple[SqlColumn, ...]
    rows: tuple[SqlRow, ...] = Field(repr=False)

    @field_validator("columns")
    @classmethod
    def require_unique_columns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("replay column names must be unique")
        return value
