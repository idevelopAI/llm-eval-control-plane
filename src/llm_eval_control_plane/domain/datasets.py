"""Immutable evaluation cases and content-addressed dataset versions."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    FiniteFloat,
    PositiveInt,
    field_validator,
    model_validator,
)

from llm_eval_control_plane.domain.artifacts import (
    ArtifactKind,
    ArtifactName,
    ArtifactRef,
    Sha256Digest,
)
from llm_eval_control_plane.domain.canonical import (
    CanonicalJson,
    JsonValue,
    canonical_json_bytes,
    sha256_digest,
)
from llm_eval_control_plane.domain.models import FrozenModel

CaseId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
SliceLabel = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/=-]*$",
    ),
]


class EvaluationCase(FrozenModel):
    """One reviewed input and its deterministic scoring expectations."""

    schema_version: Literal["1"] = "1"
    case_id: CaseId
    input: CanonicalJson = Field(repr=False)
    expected: CanonicalJson | None = Field(default=None, repr=False)
    expected_refusal: bool = False
    expected_schema: CanonicalJson | None = Field(default=None, repr=False)
    numeric_tolerance: Annotated[FiniteFloat, Field(ge=0)] | None = None
    slices: tuple[SliceLabel, ...] = ()

    @field_validator("slices")
    @classmethod
    def normalize_slices(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("slice labels must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_expectations(self) -> Self:
        if self.numeric_tolerance is not None:
            if self.expected is None:
                raise ValueError("numeric tolerance requires an expected value")
            expected = self.expected.to_value()
            if isinstance(expected, bool) or not isinstance(expected, (int, float)):
                raise ValueError("numeric tolerance requires a numeric expectation")
        if self.expected_schema is not None and not isinstance(
            self.expected_schema.to_value(), dict
        ):
            raise ValueError("expected schema must be a JSON object")
        return self

    def semantic_record(self) -> dict[str, JsonValue]:
        """Return every case field observable by targets or evaluators."""
        return {
            "case_id": self.case_id,
            "expected": None if self.expected is None else self.expected.to_value(),
            "expected_refusal": self.expected_refusal,
            "expected_schema": (
                None
                if self.expected_schema is None
                else self.expected_schema.to_value()
            ),
            "input": self.input.to_value(),
            "numeric_tolerance": self.numeric_tolerance,
            "schema_version": self.schema_version,
            "slices": list(self.slices),
        }


def dataset_content(cases: tuple[EvaluationCase, ...]) -> dict[str, JsonValue]:
    """Build the versioned semantic envelope covered by a dataset digest."""
    ordered = sorted(cases, key=lambda case: case.case_id)
    return {
        "cases": [case.semantic_record() for case in ordered],
        "digest_schema": "dataset/v1",
    }


def calculate_dataset_digest(cases: tuple[EvaluationCase, ...]) -> str:
    """Calculate the dataset content hash independent of authoring order."""
    return sha256_digest(dataset_content(cases))


class DatasetVersion(FrozenModel):
    """An immutable, verified set of evaluation cases keyed by case ID."""

    schema_version: Literal["1"] = "1"
    name: ArtifactName
    revision: PositiveInt
    digest: Sha256Digest
    cases: Annotated[tuple[EvaluationCase, ...], Field(min_length=1)]

    @field_validator("cases")
    @classmethod
    def normalize_cases(
        cls, value: tuple[EvaluationCase, ...]
    ) -> tuple[EvaluationCase, ...]:
        case_ids = [case.case_id for case in value]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("dataset case IDs must be unique")
        return tuple(sorted(value, key=lambda case: case.case_id))

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        if self.digest != calculate_dataset_digest(self.cases):
            raise ValueError("dataset digest does not match canonical case content")
        return self

    @classmethod
    def create(
        cls,
        *,
        name: str,
        revision: int,
        cases: tuple[EvaluationCase, ...],
    ) -> DatasetVersion:
        """Normalize cases and calculate their verified content digest."""
        return cls(
            name=name,
            revision=revision,
            digest=calculate_dataset_digest(cases),
            cases=cases,
        )

    @property
    def artifact_ref(self) -> ArtifactRef:
        """Return the resolved artifact identity for run manifests."""
        return ArtifactRef(
            kind=ArtifactKind.DATASET,
            name=self.name,
            revision=self.revision,
            digest=self.digest,
        )

    def canonical_content_bytes(self) -> bytes:
        """Return the exact bytes covered by ``digest`` for audit tooling."""
        return canonical_json_bytes(dataset_content(self.cases))
