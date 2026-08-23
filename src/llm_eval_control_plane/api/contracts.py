"""Strict public request and redacted response contracts for API v1."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    JsonValue,
    StrictBool,
    field_validator,
    model_validator,
)

from llm_eval_control_plane.adapters.scorers import BuiltInEvaluatorKind
from llm_eval_control_plane.domain.artifacts import ArtifactRef
from llm_eval_control_plane.domain.canonical import CanonicalJson
from llm_eval_control_plane.domain.comparison import (
    AggregateComparison,
    GateResult,
    ReleaseStatus,
)
from llm_eval_control_plane.domain.control_plane import (
    DatasetRecord,
    JobKind,
    JobRecord,
    JobStatus,
    ReleaseDecisionRecord,
    RunRecord,
)
from llm_eval_control_plane.domain.datasets import DatasetVersion, EvaluationCase
from llm_eval_control_plane.domain.evaluation import (
    EvaluationSpec,
    MetricGate,
    MetricName,
)
from llm_eval_control_plane.domain.results import (
    CaseResultStatus,
    ExecutionMode,
    RunStatus,
)

_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$"
_CASE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$"
_SLICE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/=-]*$"

ArtifactNameInput = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=_NAME_PATTERN, strict=True),
]
PositiveIntInput = Annotated[int, Field(gt=0, strict=True)]
CaseIdInput = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=_CASE_ID_PATTERN, strict=True),
]
SliceInput = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=_SLICE_PATTERN, strict=True),
]


class ApiModel(BaseModel):
    """Reject unknown public fields and keep serialization deterministic."""

    model_config = ConfigDict(extra="forbid", validate_default=True)


class EvaluationCaseInput(ApiModel):
    """Authoring-friendly case input; raw values never appear in responses."""

    schema_version: Literal["1"] = "1"
    case_id: CaseIdInput
    input: JsonValue = Field(repr=False)
    expected: JsonValue = Field(default=None, repr=False)
    expected_refusal: StrictBool = False
    expected_schema: JsonValue = Field(default=None, repr=False)
    numeric_tolerance: Annotated[FiniteFloat, Field(ge=0)] | None = None
    slices: Annotated[tuple[SliceInput, ...], Field(max_length=32)] = ()

    @field_validator("slices")
    @classmethod
    def validate_slices(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("slice labels must be unique")
        return tuple(sorted(value))

    def to_domain(self) -> EvaluationCase:
        expected = (
            CanonicalJson.from_value(self.expected)
            if "expected" in self.model_fields_set
            else None
        )
        return EvaluationCase(
            schema_version=self.schema_version,
            case_id=self.case_id,
            input=CanonicalJson.from_value(self.input),
            expected=expected,
            expected_refusal=self.expected_refusal,
            expected_schema=(
                None
                if self.expected_schema is None
                else CanonicalJson.from_value(self.expected_schema)
            ),
            numeric_tolerance=self.numeric_tolerance,
            slices=self.slices,
        )


class DatasetCreateRequest(ApiModel):
    name: ArtifactNameInput
    revision: PositiveIntInput
    cases: Annotated[
        tuple[EvaluationCaseInput, ...],
        Field(min_length=1, max_length=1_000),
    ]

    @model_validator(mode="after")
    def validate_unique_slices(self) -> Self:
        slices = {label for case in self.cases for label in case.slices}
        if len(slices) > 128:
            raise ValueError("dataset contains too many unique slices")
        return self

    def to_domain(self) -> DatasetVersion:
        return DatasetVersion.create(
            name=self.name,
            revision=self.revision,
            cases=tuple(case.to_domain() for case in self.cases),
        )


class DatasetResponse(ApiModel):
    schema_version: Literal["dataset-summary/v1"] = "dataset-summary/v1"
    name: str
    revision: int
    digest: str
    case_count: int
    created_at: datetime

    @classmethod
    def from_record(cls, record: DatasetRecord) -> Self:
        return cls(
            name=record.dataset.name,
            revision=record.dataset.revision,
            digest=record.dataset.digest,
            case_count=len(record.dataset.cases),
            created_at=record.created_at,
        )


class DatasetPage(ApiModel):
    schema_version: Literal["dataset-page/v1"] = "dataset-page/v1"
    items: tuple[DatasetResponse, ...]
    next_cursor: str | None = None


class RunCreateRequest(ApiModel):
    dataset_name: ArtifactNameInput
    dataset_revision: PositiveIntInput
    target_name: ArtifactNameInput = "fake/deterministic"
    target_revision: PositiveIntInput = 1
    adapter: Literal["deterministic_fake"] = "deterministic_fake"
    evaluators: Annotated[
        tuple[BuiltInEvaluatorKind, ...],
        Field(min_length=1, max_length=len(BuiltInEvaluatorKind)),
    ]
    scenario_overrides: Annotated[
        dict[
            CaseIdInput,
            Literal[
                "echo",
                "malformed",
                "mismatch",
                "missing_usage",
                "offset",
                "raise",
                "refuse",
                "uppercase",
            ],
        ],
        Field(max_length=1_000),
    ] = Field(default_factory=dict)

    @field_validator("evaluators")
    @classmethod
    def validate_evaluators(
        cls,
        value: tuple[BuiltInEvaluatorKind, ...],
    ) -> tuple[BuiltInEvaluatorKind, ...]:
        if len(value) != len(set(value)):
            raise ValueError("evaluator kinds must be unique")
        return tuple(sorted(value, key=lambda item: item.value))


class JobResponse(ApiModel):
    schema_version: Literal["job/v1"] = "job/v1"
    job_id: str
    kind: JobKind
    status: JobStatus
    resource_id: str
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: JobRecord) -> Self:
        return cls(
            job_id=record.job_id,
            kind=record.kind,
            status=record.status,
            resource_id=record.resource_id,
            error_code=record.error_code,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class JobPage(ApiModel):
    schema_version: Literal["job-page/v1"] = "job-page/v1"
    items: tuple[JobResponse, ...]
    next_cursor: str | None = None


class MetricSummaryResponse(ApiModel):
    metric: MetricName
    evaluator: ArtifactRef
    attempted: int
    scored: int
    skipped: int
    errors: int
    mean: float | None = None


class CaseStatusCounts(ApiModel):
    completed: int
    completed_with_errors: int
    target_failed: int


class RunResponse(ApiModel):
    schema_version: Literal["run-summary/v1"] = "run-summary/v1"
    run_id: str
    status: RunStatus
    execution_mode: ExecutionMode
    dataset: ArtifactRef
    target: ArtifactRef
    evaluators: tuple[ArtifactRef, ...]
    case_status_counts: CaseStatusCounts
    metrics: tuple[MetricSummaryResponse, ...]
    result_digest: str
    created_at: datetime

    @classmethod
    def from_record(cls, record: RunRecord) -> Self:
        result = record.result
        counts = dict.fromkeys(CaseResultStatus, 0)
        for case in result.cases:
            counts[case.status] += 1
        return cls(
            run_id=result.run_id,
            status=result.status,
            execution_mode=result.execution_mode,
            dataset=result.dataset,
            target=result.target,
            evaluators=result.evaluators,
            case_status_counts=CaseStatusCounts(
                completed=counts[CaseResultStatus.COMPLETED],
                completed_with_errors=counts[CaseResultStatus.COMPLETED_WITH_ERRORS],
                target_failed=counts[CaseResultStatus.TARGET_FAILED],
            ),
            metrics=tuple(
                MetricSummaryResponse.model_validate(summary.model_dump())
                for summary in result.metrics
            ),
            result_digest=result.result_digest,
            created_at=record.created_at,
        )


class RunPage(ApiModel):
    schema_version: Literal["run-page/v1"] = "run-page/v1"
    items: tuple[RunResponse, ...]
    next_cursor: str | None = None


class RunSubmissionResponse(ApiModel):
    schema_version: Literal["run-submission/v1"] = "run-submission/v1"
    job: JobResponse
    run: RunResponse | None = None


class EvaluationSpecInput(EvaluationSpec):
    """Public comparison policy with an explicit bounded gate collection."""

    gates: Annotated[
        tuple[MetricGate, ...],
        Field(min_length=1, max_length=64),
    ]


class ComparisonCreateRequest(ApiModel):
    dataset_name: ArtifactNameInput
    dataset_revision: PositiveIntInput
    baseline_run_id: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
            strict=True,
        ),
    ]
    candidate_run_id: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
            strict=True,
        ),
    ]
    spec: EvaluationSpecInput


class ReleaseDecisionResponse(ApiModel):
    schema_version: Literal["release-decision-summary/v1"] = (
        "release-decision-summary/v1"
    )
    decision_id: str
    status: ReleaseStatus
    spec_name: str
    execution_mode: ExecutionMode
    dataset: ArtifactRef
    baseline: ArtifactRef
    candidate: ArtifactRef
    baseline_run_id: str
    candidate_run_id: str
    baseline_result_digest: str
    candidate_result_digest: str
    aggregates: tuple[AggregateComparison, ...]
    gates: tuple[GateResult, ...]
    decision_digest: str
    created_at: datetime

    @classmethod
    def from_record(cls, record: ReleaseDecisionRecord) -> Self:
        decision = record.decision
        return cls(
            decision_id=record.decision_id,
            status=decision.status,
            spec_name=decision.spec_name,
            execution_mode=decision.execution_mode,
            dataset=decision.dataset,
            baseline=decision.baseline,
            candidate=decision.candidate,
            baseline_run_id=decision.baseline_run_id,
            candidate_run_id=decision.candidate_run_id,
            baseline_result_digest=decision.baseline_result_digest,
            candidate_result_digest=decision.candidate_result_digest,
            aggregates=decision.aggregates,
            gates=decision.gates,
            decision_digest=decision.decision_digest,
            created_at=record.created_at,
        )


class ReleaseDecisionPage(ApiModel):
    schema_version: Literal["release-decision-page/v1"] = "release-decision-page/v1"
    items: tuple[ReleaseDecisionResponse, ...]
    next_cursor: str | None = None


class ComparisonSubmissionResponse(ApiModel):
    schema_version: Literal["comparison-submission/v1"] = "comparison-submission/v1"
    job: JobResponse
    decision: ReleaseDecisionResponse | None = None


class ErrorDetail(ApiModel):
    location: tuple[str | int, ...]
    type: str


class ApiError(ApiModel):
    code: str
    message: str
    request_id: str
    details: tuple[ErrorDetail, ...] = ()


class ApiErrorDocument(ApiModel):
    schema_version: Literal["api-error/v1"] = "api-error/v1"
    error: ApiError


class HealthResponse(ApiModel):
    schema_version: Literal["health/v1"] = "health/v1"
    status: Literal["ok", "unavailable"]


__all__ = [
    "ApiErrorDocument",
    "ComparisonCreateRequest",
    "ComparisonSubmissionResponse",
    "DatasetCreateRequest",
    "DatasetPage",
    "DatasetResponse",
    "ErrorDetail",
    "EvaluationSpecInput",
    "HealthResponse",
    "JobPage",
    "JobResponse",
    "ReleaseDecisionPage",
    "ReleaseDecisionResponse",
    "RunCreateRequest",
    "RunPage",
    "RunResponse",
    "RunSubmissionResponse",
]
