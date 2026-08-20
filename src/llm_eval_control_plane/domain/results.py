"""Immutable case evidence, aggregate metrics, and complete run results."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, FiniteFloat, NonNegativeInt, PositiveInt, model_validator

from llm_eval_control_plane.domain.artifacts import (
    ArtifactKind,
    ArtifactRef,
    Sha256Digest,
)
from llm_eval_control_plane.domain.canonical import JsonValue, sha256_digest
from llm_eval_control_plane.domain.datasets import CaseId
from llm_eval_control_plane.domain.evaluation import MetricName
from llm_eval_control_plane.domain.execution import (
    ErrorObservation,
    ExecutionFailure,
    FailureStage,
    MetricObservation,
    ObservationStatus,
    RunId,
    ScoredObservation,
    SkippedObservation,
    TargetObservation,
)
from llm_eval_control_plane.domain.models import FrozenModel


class CaseResultStatus(StrEnum):
    COMPLETED = "completed"
    TARGET_FAILED = "target_failed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"


class CaseResult(FrozenModel):
    """Append-only evidence for one target invocation and all evaluators."""

    case_id: CaseId
    status: CaseResultStatus
    target: TargetObservation | None = Field(default=None, repr=False)
    target_failure: ExecutionFailure | None = None
    observations: tuple[MetricObservation, ...] = ()
    evaluator_failures: tuple[ExecutionFailure, ...] = ()

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if self.status is CaseResultStatus.TARGET_FAILED:
            if self.target is not None or self.target_failure is None:
                raise ValueError("target-failed cases require only a target failure")
            if self.target_failure.stage is not FailureStage.TARGET:
                raise ValueError("target failure must use the target stage")
            if self.observations or self.evaluator_failures:
                raise ValueError("target-failed cases cannot contain evaluator results")
        else:
            if self.target is None or self.target_failure is not None:
                raise ValueError("completed cases require a target observation")
            if self.status is CaseResultStatus.COMPLETED and self.evaluator_failures:
                raise ValueError("completed cases cannot contain evaluator failures")
            if (
                self.status is CaseResultStatus.COMPLETED_WITH_ERRORS
                and not self.evaluator_failures
                and not any(
                    observation.status is ObservationStatus.ERROR
                    for observation in self.observations
                )
            ):
                raise ValueError("completed-with-errors cases require evaluator errors")

        evaluator_failures = [failure.evaluator for failure in self.evaluator_failures]
        if any(
            failure.stage is not FailureStage.EVALUATOR
            for failure in self.evaluator_failures
        ):
            raise ValueError("case evaluator failures must use the evaluator stage")
        if len(evaluator_failures) != len(set(evaluator_failures)):
            raise ValueError("case evaluator failures must be unique")
        failure_keys = [
            failure.evaluator.logical_key
            for failure in self.evaluator_failures
            if failure.evaluator is not None
        ]
        if failure_keys != sorted(failure_keys):
            raise ValueError("case evaluator failures must be canonically ordered")

        observation_keys = [
            (observation.evaluator.logical_key, observation.metric)
            for observation in self.observations
        ]
        if len(observation_keys) != len(set(observation_keys)):
            raise ValueError("case observations must have unique evaluator metrics")
        if observation_keys != sorted(observation_keys):
            raise ValueError("case observations must be canonically ordered")
        return self


class MetricSummary(FrozenModel):
    """Coverage-aware aggregate for one evaluator metric."""

    metric: MetricName
    evaluator: ArtifactRef
    attempted: PositiveInt
    scored: NonNegativeInt
    skipped: NonNegativeInt
    errors: NonNegativeInt
    mean: FiniteFloat | None = None

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.evaluator.kind is not ArtifactKind.EVALUATOR:
            raise ValueError("summary evaluator must reference an evaluator artifact")
        if self.evaluator.digest is None:
            raise ValueError("summary evaluator must have a resolved digest")
        if self.attempted != self.scored + self.skipped + self.errors:
            raise ValueError("summary outcome counts must equal attempted cases")
        if (self.scored == 0) != (self.mean is None):
            raise ValueError(
                "summary mean exists exactly when observations were scored"
            )
        return self


class RunStatus(StrEnum):
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"


def _artifact_record(artifact: ArtifactRef) -> dict[str, JsonValue]:
    return {
        "digest": artifact.digest,
        "kind": artifact.kind.value,
        "name": artifact.name,
        "revision": artifact.revision,
    }


def _observation_record(observation: MetricObservation) -> dict[str, JsonValue]:
    base: dict[str, JsonValue] = {
        "evaluator": _artifact_record(observation.evaluator),
        "metric": observation.metric,
        "status": observation.status.value,
    }
    if isinstance(observation, ScoredObservation):
        base.update(value=observation.value, reason_code=observation.reason_code)
    elif isinstance(observation, SkippedObservation):
        base.update(reason_code=observation.reason_code)
    elif isinstance(observation, ErrorObservation):
        base.update(error_code=observation.error_code, message=observation.message)
    return base


def _failure_record(failure: ExecutionFailure) -> dict[str, JsonValue]:
    return {
        "code": failure.code.value,
        "evaluator": (
            None if failure.evaluator is None else _artifact_record(failure.evaluator)
        ),
        "latency_ms": failure.latency_ms,
        "message": failure.message,
        "retryable": failure.retryable,
        "stage": failure.stage.value,
    }


def _case_record(case: CaseResult) -> dict[str, JsonValue]:
    target: dict[str, JsonValue] | None = None
    if case.target is not None:
        target = {
            "latency_ms": case.target.latency_ms,
            "outcome": case.target.response.outcome.value,
            "output": case.target.response.output.to_value(),
            "refusal_code": case.target.response.refusal_code,
            "usage": {
                "input_units": case.target.response.usage.input_units,
                "output_units": case.target.response.usage.output_units,
            },
        }
    return {
        "case_id": case.case_id,
        "evaluator_failures": [
            _failure_record(failure) for failure in case.evaluator_failures
        ],
        "observations": [
            _observation_record(observation) for observation in case.observations
        ],
        "status": case.status.value,
        "target": target,
        "target_failure": (
            None
            if case.target_failure is None
            else _failure_record(case.target_failure)
        ),
    }


def _summary_record(summary: MetricSummary) -> dict[str, JsonValue]:
    return {
        "attempted": summary.attempted,
        "errors": summary.errors,
        "evaluator": _artifact_record(summary.evaluator),
        "mean": summary.mean,
        "metric": summary.metric,
        "scored": summary.scored,
        "skipped": summary.skipped,
    }


def calculate_run_digest(
    *,
    dataset: ArtifactRef,
    target: ArtifactRef,
    evaluators: tuple[ArtifactRef, ...],
    cases: tuple[CaseResult, ...],
    metrics: tuple[MetricSummary, ...],
) -> str:
    """Hash the stable run content projection, excluding the caller's run ID."""
    return sha256_digest(
        {
            "cases": [_case_record(case) for case in cases],
            "dataset": _artifact_record(dataset),
            "digest_schema": "run-result/v1",
            "evaluators": [_artifact_record(evaluator) for evaluator in evaluators],
            "metrics": [_summary_record(summary) for summary in metrics],
            "target": _artifact_record(target),
        }
    )


class RunResult(FrozenModel):
    """A complete deterministic execution artifact suitable for persistence."""

    run_id: RunId
    status: RunStatus
    dataset: ArtifactRef
    target: ArtifactRef
    evaluators: Annotated[tuple[ArtifactRef, ...], Field(min_length=1)]
    cases: Annotated[tuple[CaseResult, ...], Field(min_length=1)]
    metrics: Annotated[tuple[MetricSummary, ...], Field(min_length=1)]
    result_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.dataset.kind is not ArtifactKind.DATASET or self.dataset.digest is None:
            raise ValueError("run dataset must be a resolved dataset artifact")
        if self.target.kind is not ArtifactKind.TARGET or self.target.digest is None:
            raise ValueError("run target must be a resolved target artifact")
        if any(
            evaluator.kind is not ArtifactKind.EVALUATOR or evaluator.digest is None
            for evaluator in self.evaluators
        ):
            raise ValueError("run evaluators must be resolved evaluator artifacts")
        evaluator_keys = [evaluator.logical_key for evaluator in self.evaluators]
        if len(evaluator_keys) != len(set(evaluator_keys)):
            raise ValueError("run evaluator references must be unique")
        if evaluator_keys != sorted(evaluator_keys):
            raise ValueError("run evaluator references must be canonically ordered")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("run case results must be unique")
        if case_ids != sorted(case_ids):
            raise ValueError("run case results must be canonically ordered")
        metric_keys = [
            (summary.metric, summary.evaluator.logical_key) for summary in self.metrics
        ]
        if metric_keys != sorted(metric_keys):
            raise ValueError("run metric summaries must be canonically ordered")

        has_failures = any(
            case.status is not CaseResultStatus.COMPLETED for case in self.cases
        ) or any(summary.errors > 0 for summary in self.metrics)
        expected_status = (
            RunStatus.COMPLETED_WITH_FAILURES if has_failures else RunStatus.COMPLETED
        )
        if self.status is not expected_status:
            raise ValueError("run status does not match case and metric outcomes")
        expected_digest = calculate_run_digest(
            dataset=self.dataset,
            target=self.target,
            evaluators=self.evaluators,
            cases=self.cases,
            metrics=self.metrics,
        )
        if self.result_digest != expected_digest:
            raise ValueError("run result digest does not match canonical content")
        return self

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        dataset: ArtifactRef,
        target: ArtifactRef,
        evaluators: tuple[ArtifactRef, ...],
        cases: tuple[CaseResult, ...],
        metrics: tuple[MetricSummary, ...],
    ) -> RunResult:
        """Create a sorted, content-addressed complete result."""
        ordered_evaluators = tuple(
            sorted(evaluators, key=lambda item: item.logical_key)
        )
        ordered_cases = tuple(sorted(cases, key=lambda item: item.case_id))
        ordered_metrics = tuple(
            sorted(metrics, key=lambda item: (item.metric, item.evaluator.logical_key))
        )
        status = (
            RunStatus.COMPLETED_WITH_FAILURES
            if any(
                case.status is not CaseResultStatus.COMPLETED for case in ordered_cases
            )
            or any(summary.errors > 0 for summary in ordered_metrics)
            else RunStatus.COMPLETED
        )
        return cls(
            run_id=run_id,
            status=status,
            dataset=dataset,
            target=target,
            evaluators=ordered_evaluators,
            cases=ordered_cases,
            metrics=ordered_metrics,
            result_digest=calculate_run_digest(
                dataset=dataset,
                target=target,
                evaluators=ordered_evaluators,
                cases=ordered_cases,
                metrics=ordered_metrics,
            ),
        )
