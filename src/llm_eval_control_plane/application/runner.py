"""Serial, in-process evaluation orchestration with sanitized failures."""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from pydantic import TypeAdapter, ValidationError

from llm_eval_control_plane.application.ports import EvaluatorPort, TargetPort
from llm_eval_control_plane.domain import (
    ArtifactKind,
    CaseResult,
    CaseResultStatus,
    DatasetVersion,
    ErrorObservation,
    ExecutionFailure,
    FailureCode,
    FailureStage,
    MetricSummary,
    RunResult,
    ScoredObservation,
    SkippedObservation,
    TargetObservation,
    TargetRequest,
    TargetResponse,
)
from llm_eval_control_plane.domain.execution import MetricObservation

Clock = Callable[[], float]


class RunnerConfigurationError(ValueError):
    """A safe preflight failure before any target invocation occurs."""


class InProcessRunner:
    """Run one dataset serially before durable queues are introduced."""

    def __init__(self, *, clock: Clock = time.perf_counter) -> None:
        self._clock = clock
        self._response_adapter: TypeAdapter[TargetResponse] = TypeAdapter(
            TargetResponse
        )
        self._observation_adapter: TypeAdapter[tuple[MetricObservation, ...]] = (
            TypeAdapter(tuple[MetricObservation, ...])
        )

    async def run(
        self,
        *,
        run_id: str,
        dataset: DatasetVersion,
        target: TargetPort,
        evaluators: tuple[EvaluatorPort, ...],
    ) -> RunResult:
        """Execute every case and return a complete immutable result."""
        ordered_evaluators = self._validate_plan(target, evaluators)
        case_results: list[CaseResult] = []
        for case in dataset.cases:
            request = TargetRequest(case_id=case.case_id, input=case.input)
            started = self._clock()
            try:
                raw_response = await target.invoke(request)
            except Exception:
                latency_ms = self._elapsed_ms(started)
                case_results.append(
                    CaseResult(
                        case_id=case.case_id,
                        status=CaseResultStatus.TARGET_FAILED,
                        target_failure=ExecutionFailure(
                            stage=FailureStage.TARGET,
                            code=FailureCode.TARGET_EXCEPTION,
                            message="Target raised an exception",
                            latency_ms=latency_ms,
                        ),
                    )
                )
                continue

            latency_ms = self._elapsed_ms(started)
            try:
                response = self._response_adapter.validate_python(raw_response)
            except ValidationError:
                case_results.append(
                    CaseResult(
                        case_id=case.case_id,
                        status=CaseResultStatus.TARGET_FAILED,
                        target_failure=ExecutionFailure(
                            stage=FailureStage.TARGET,
                            code=FailureCode.INVALID_TARGET_OUTPUT,
                            message="Target result failed contract validation",
                            latency_ms=latency_ms,
                        ),
                    )
                )
                continue

            target_observation = TargetObservation(
                response=response,
                latency_ms=latency_ms,
            )
            observations: list[MetricObservation] = []
            evaluator_failures: list[ExecutionFailure] = []
            for evaluator in ordered_evaluators:
                try:
                    raw_observations = evaluator.evaluate(case, target_observation)
                except Exception:
                    evaluator_failures.append(
                        self._evaluator_failure(
                            evaluator,
                            FailureCode.EVALUATOR_EXCEPTION,
                            "Evaluator raised an exception",
                        )
                    )
                    continue
                try:
                    validated = self._observation_adapter.validate_python(
                        raw_observations
                    )
                    self._validate_observations(evaluator, validated)
                except (ValidationError, RunnerConfigurationError):
                    evaluator_failures.append(
                        self._evaluator_failure(
                            evaluator,
                            FailureCode.INVALID_EVALUATOR_OUTPUT,
                            "Evaluator result failed contract validation",
                        )
                    )
                    continue
                observations.extend(
                    sorted(
                        validated,
                        key=lambda observation: (
                            observation.evaluator.logical_key,
                            observation.metric,
                        ),
                    )
                )

            has_errors = bool(evaluator_failures) or any(
                isinstance(observation, ErrorObservation)
                for observation in observations
            )
            case_results.append(
                CaseResult(
                    case_id=case.case_id,
                    status=(
                        CaseResultStatus.COMPLETED_WITH_ERRORS
                        if has_errors
                        else CaseResultStatus.COMPLETED
                    ),
                    target=target_observation,
                    observations=tuple(observations),
                    evaluator_failures=tuple(evaluator_failures),
                )
            )

        cases = tuple(case_results)
        summaries = self._summarize(cases=cases, evaluators=ordered_evaluators)
        return RunResult.create(
            run_id=run_id,
            dataset=dataset.artifact_ref,
            target=target.ref,
            evaluators=tuple(evaluator.ref for evaluator in ordered_evaluators),
            cases=cases,
            metrics=summaries,
        )

    def _elapsed_ms(self, started: float) -> float:
        finished = self._clock()
        elapsed = (finished - started) * 1_000
        if not math.isfinite(elapsed) or elapsed < 0:
            raise RunnerConfigurationError("monotonic clock returned invalid time")
        return round(elapsed, 6)

    @staticmethod
    def _validate_plan(
        target: TargetPort,
        evaluators: tuple[EvaluatorPort, ...],
    ) -> tuple[EvaluatorPort, ...]:
        if target.ref.kind is not ArtifactKind.TARGET or target.ref.digest is None:
            raise RunnerConfigurationError(
                "target must have a resolved target reference"
            )
        if not evaluators:
            raise RunnerConfigurationError("at least one evaluator is required")
        ordered = tuple(sorted(evaluators, key=lambda item: item.ref.logical_key))
        evaluator_keys = [evaluator.ref.logical_key for evaluator in ordered]
        if len(evaluator_keys) != len(set(evaluator_keys)):
            raise RunnerConfigurationError("evaluator references must be unique")

        metric_names: list[str] = []
        for evaluator in ordered:
            if (
                evaluator.ref.kind is not ArtifactKind.EVALUATOR
                or evaluator.ref.digest is None
            ):
                raise RunnerConfigurationError(
                    "evaluators must have resolved evaluator references"
                )
            if not evaluator.metric_names:
                raise RunnerConfigurationError("evaluators must declare metrics")
            if len(evaluator.metric_names) != len(set(evaluator.metric_names)):
                raise RunnerConfigurationError("evaluator metrics must be unique")
            metric_names.extend(evaluator.metric_names)
        if len(metric_names) != len(set(metric_names)):
            raise RunnerConfigurationError("run metric names must be unique")
        return ordered

    @staticmethod
    def _validate_observations(
        evaluator: EvaluatorPort,
        observations: tuple[MetricObservation, ...],
    ) -> None:
        if {observation.metric for observation in observations} != set(
            evaluator.metric_names
        ):
            raise RunnerConfigurationError(
                "evaluator observations must cover every declared metric"
            )
        if any(
            observation.evaluator.logical_key != evaluator.ref.logical_key
            or observation.evaluator.digest != evaluator.ref.digest
            for observation in observations
        ):
            raise RunnerConfigurationError(
                "evaluator observations must use the configured evaluator reference"
            )

    @staticmethod
    def _evaluator_failure(
        evaluator: EvaluatorPort,
        code: FailureCode,
        message: str,
    ) -> ExecutionFailure:
        return ExecutionFailure(
            stage=FailureStage.EVALUATOR,
            code=code,
            message=message,
            evaluator=evaluator.ref,
        )

    @staticmethod
    def _summarize(
        *,
        cases: tuple[CaseResult, ...],
        evaluators: tuple[EvaluatorPort, ...],
    ) -> tuple[MetricSummary, ...]:
        summaries: list[MetricSummary] = []
        for evaluator in evaluators:
            for metric in evaluator.metric_names:
                values: list[float] = []
                skipped = 0
                errors = 0
                for case in cases:
                    if case.status is CaseResultStatus.TARGET_FAILED:
                        errors += 1
                        continue
                    evaluator_failed = any(
                        failure.evaluator is not None
                        and failure.evaluator.logical_key == evaluator.ref.logical_key
                        for failure in case.evaluator_failures
                    )
                    if evaluator_failed:
                        errors += 1
                        continue
                    observation = next(
                        (
                            item
                            for item in case.observations
                            if item.metric == metric
                            and item.evaluator.logical_key == evaluator.ref.logical_key
                        ),
                        None,
                    )
                    if isinstance(observation, ScoredObservation):
                        values.append(observation.value)
                    elif isinstance(observation, SkippedObservation):
                        skipped += 1
                    else:
                        errors += 1
                summaries.append(
                    MetricSummary(
                        metric=metric,
                        evaluator=evaluator.ref,
                        attempted=len(cases),
                        scored=len(values),
                        skipped=skipped,
                        errors=errors,
                        mean=math.fsum(values) / len(values) if values else None,
                    )
                )
        return tuple(summaries)
