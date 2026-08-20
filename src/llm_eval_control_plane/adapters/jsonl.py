"""Strict JSONL transport for reviewed, content-addressed datasets."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Literal, TextIO

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    ValidationError,
)
from pydantic import JsonValue as PydanticJsonValue

from llm_eval_control_plane.domain.canonical import (
    CanonicalJson,
    CanonicalJsonError,
    canonical_json_bytes,
    parse_json,
)
from llm_eval_control_plane.domain.datasets import (
    CaseId,
    DatasetVersion,
    EvaluationCase,
    SliceLabel,
)


class DatasetImportError(ValueError):
    """A structured, content-safe JSONL import failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.line = line
        self.column = column


class _CaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    case_id: CaseId
    input: PydanticJsonValue
    expected: PydanticJsonValue = None
    expected_refusal: bool = False
    expected_schema: PydanticJsonValue = None
    numeric_tolerance: Annotated[FiniteFloat, Field(ge=0)] | None = None
    slices: tuple[SliceLabel, ...] = ()

    def to_domain(self) -> EvaluationCase:
        return EvaluationCase(
            schema_version=self.schema_version,
            case_id=self.case_id,
            input=CanonicalJson.from_value(self.input),
            expected=(
                None
                if self.expected is None
                else CanonicalJson.from_value(self.expected)
            ),
            expected_refusal=self.expected_refusal,
            expected_schema=(
                None
                if self.expected_schema is None
                else CanonicalJson.from_value(self.expected_schema)
            ),
            numeric_tolerance=self.numeric_tolerance,
            slices=self.slices,
        )


def _parse_case(line_text: str, line_number: int) -> EvaluationCase:
    try:
        value = parse_json(line_text)
    except CanonicalJsonError as error:
        raise DatasetImportError(
            error.code,
            str(error),
            line=line_number,
            column=error.column,
        ) from error
    if not isinstance(value, dict):
        raise DatasetImportError(
            "case_not_object",
            "Each JSONL record must be a JSON object",
            line=line_number,
        )
    try:
        record = _CaseRecord.model_validate(value)
        return record.to_domain()
    except (ValidationError, CanonicalJsonError) as error:
        raise DatasetImportError(
            "invalid_case",
            "JSONL case failed contract validation",
            line=line_number,
        ) from error


def import_dataset_jsonl(
    lines: Iterable[str],
    *,
    name: str,
    revision: int,
) -> DatasetVersion:
    """Import a reviewed dataset from UTF-8 text lines."""
    cases: list[EvaluationCase] = []
    first_lines: dict[str, int] = {}
    for line_number, line_text in enumerate(lines, start=1):
        if not line_text.strip():
            continue
        item = _parse_case(line_text, line_number)
        if item.case_id in first_lines:
            raise DatasetImportError(
                "duplicate_case_id",
                "Dataset contains a duplicate case ID",
                line=line_number,
            )
        first_lines[item.case_id] = line_number
        cases.append(item)
    if not cases:
        raise DatasetImportError("empty_dataset", "Dataset contains no cases")
    try:
        return DatasetVersion.create(name=name, revision=revision, cases=tuple(cases))
    except ValidationError as error:
        raise DatasetImportError(
            "invalid_dataset",
            "Dataset metadata failed contract validation",
        ) from error


def export_dataset_jsonl(dataset: DatasetVersion) -> str:
    """Export normalized cases in case-ID order with one final newline."""
    records = (
        canonical_json_bytes(item.semantic_record()).decode("utf-8")
        for item in dataset.cases
    )
    return "\n".join(records) + "\n"


def read_dataset_jsonl(
    path: Path,
    *,
    name: str,
    revision: int,
) -> DatasetVersion:
    """Read a UTF-8 JSONL path without exposing invalid content in errors."""
    try:
        with path.open(encoding="utf-8") as stream:
            return import_dataset_jsonl(stream, name=name, revision=revision)
    except UnicodeDecodeError as error:
        raise DatasetImportError(
            "invalid_utf8",
            "Dataset must be valid UTF-8",
        ) from error


def write_dataset_jsonl(dataset: DatasetVersion, stream: TextIO) -> None:
    """Write normalized JSONL to a caller-owned text stream."""
    stream.write(export_dataset_jsonl(dataset))
