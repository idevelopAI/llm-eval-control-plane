"""Provider-neutral target request and response envelopes."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, FiniteFloat, StrictInt, model_validator

from llm_eval_control_plane.domain.artifacts import ArtifactKind, ArtifactRef
from llm_eval_control_plane.domain.canonical import CanonicalJson
from llm_eval_control_plane.domain.datasets import CaseId
from llm_eval_control_plane.domain.evaluation import MetricName
from llm_eval_control_plane.domain.models import FrozenModel

SafeCode = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]
SafeMessage = Annotated[str, Field(min_length=1, max_length=256)]
RunId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
UsageUnit = Annotated[StrictInt, Field(ge=0)]
DurationMs = Annotated[FiniteFloat, Field(ge=0)]


class TargetOutcome(StrEnum):
    """Structured target state; refusal is never inferred from wording."""

    COMPLETED = "completed"
    REFUSED = "refused"


class TokenUsage(FrozenModel):
    """Required target usage counters; missing usage is invalid output."""

    input_units: UsageUnit
    output_units: UsageUnit

    @property
    def total_units(self) -> int:
        return self.input_units + self.output_units


class TargetRequest(FrozenModel):
    """The only case data visible to a target adapter."""

    case_id: CaseId
    input: CanonicalJson = Field(repr=False)


class TargetResponse(FrozenModel):
    """Untrusted adapter output after validation at the application boundary."""

    output: CanonicalJson = Field(repr=False)
    outcome: TargetOutcome = TargetOutcome.COMPLETED
    refusal_code: SafeCode | None = None
    usage: TokenUsage

    @model_validator(mode="after")
    def validate_refusal(self) -> Self:
        if self.outcome is TargetOutcome.REFUSED and self.refusal_code is None:
            raise ValueError("refused responses require a refusal code")
        if self.outcome is TargetOutcome.COMPLETED and self.refusal_code is not None:
            raise ValueError("completed responses cannot include a refusal code")
        return self


class TargetObservation(FrozenModel):
    """A validated response paired with control-plane measured latency."""

    response: TargetResponse
    latency_ms: DurationMs


class FailureStage(StrEnum):
    TARGET = "target"
    EVALUATOR = "evaluator"


class FailureCode(StrEnum):
    TARGET_EXCEPTION = "target_exception"
    INVALID_TARGET_OUTPUT = "invalid_target_output"
    EVALUATOR_EXCEPTION = "evaluator_exception"
    INVALID_EVALUATOR_OUTPUT = "invalid_evaluator_output"


class ExecutionFailure(FrozenModel):
    """Sanitized failure evidence safe to persist and display."""

    stage: FailureStage
    code: FailureCode
    message: SafeMessage
    retryable: bool = False
    evaluator: ArtifactRef | None = None
    latency_ms: DurationMs | None = None

    @model_validator(mode="after")
    def validate_stage(self) -> Self:
        if self.stage is FailureStage.EVALUATOR:
            if self.evaluator is None:
                raise ValueError("evaluator failures require an evaluator reference")
            if self.evaluator.kind is not ArtifactKind.EVALUATOR:
                raise ValueError(
                    "failure evaluator must reference an evaluator artifact"
                )
            if self.latency_ms is not None:
                raise ValueError("evaluator failures cannot include target latency")
        elif self.evaluator is not None:
            raise ValueError("target failures cannot include an evaluator reference")
        return self


class ObservationStatus(StrEnum):
    SCORED = "scored"
    SKIPPED = "skipped"
    ERROR = "error"


class ObservationBase(FrozenModel):
    """Shared identity for one evaluator metric on one case."""

    metric: MetricName
    evaluator: ArtifactRef

    @model_validator(mode="after")
    def validate_evaluator(self) -> Self:
        if self.evaluator.kind is not ArtifactKind.EVALUATOR:
            raise ValueError(
                "observation evaluator must reference an evaluator artifact"
            )
        if self.evaluator.digest is None:
            raise ValueError("observation evaluator must have a resolved digest")
        return self


class ScoredObservation(ObservationBase):
    status: Literal[ObservationStatus.SCORED] = ObservationStatus.SCORED
    value: FiniteFloat
    reason_code: SafeCode


class SkippedObservation(ObservationBase):
    status: Literal[ObservationStatus.SKIPPED] = ObservationStatus.SKIPPED
    reason_code: SafeCode


class ErrorObservation(ObservationBase):
    status: Literal[ObservationStatus.ERROR] = ObservationStatus.ERROR
    error_code: SafeCode
    message: SafeMessage


MetricObservation = Annotated[
    ScoredObservation | SkippedObservation | ErrorObservation,
    Field(discriminator="status"),
]
