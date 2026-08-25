"""Explicit offline execution adapter used by leased control-plane workers."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from opentelemetry.trace import NoOpTracerProvider, SpanKind, Tracer

from llm_eval_control_plane.adapters.fake_target import (
    DeterministicFakeTarget,
    DeterministicStepClock,
)
from llm_eval_control_plane.adapters.scorers import (
    BuiltInEvaluatorKind,
    build_evaluators,
)
from llm_eval_control_plane.application.control_plane import ExecutionContract
from llm_eval_control_plane.application.ports import EvaluatorPort, TargetPort
from llm_eval_control_plane.application.runner import InProcessRunner
from llm_eval_control_plane.domain import (
    ArtifactRef,
    EvaluationCase,
    TargetObservation,
    TargetRequest,
)
from llm_eval_control_plane.domain.datasets import DatasetVersion
from llm_eval_control_plane.domain.evaluation import MetricName
from llm_eval_control_plane.domain.execution import MetricObservation
from llm_eval_control_plane.domain.results import ExecutionMode, RunResult

_NOOP_TRACER = NoOpTracerProvider().get_tracer("llm_eval_control_plane.api.execution")


class DeterministicEvaluationExecutor:
    """Run only the credential-free deterministic adapter exposed by API v1."""

    _ADAPTER = "deterministic_fake"

    def __init__(self, *, tracer: Tracer = _NOOP_TRACER) -> None:
        self._tracer = tracer

    def validate(
        self,
        *,
        target_name: str,
        target_revision: int,
        adapter: str,
        evaluator_names: tuple[str, ...],
        scenario_overrides: Mapping[str, str],
    ) -> ExecutionContract:
        if adapter != self._ADAPTER:
            raise ValueError("unsupported target adapter")
        kinds = self._evaluator_kinds(evaluator_names)
        evaluators = build_evaluators(kinds)
        target = DeterministicFakeTarget(
            name=target_name,
            revision=target_revision,
            scenario_overrides=scenario_overrides,
        )
        return ExecutionContract(
            adapter=self._ADAPTER,
            evaluator_names=evaluator_names,
            target=target.ref,
            evaluators=tuple(evaluator.ref for evaluator in evaluators),
            execution_mode=ExecutionMode.OFFLINE_MOCK,
        )

    async def execute(
        self,
        *,
        run_id: str,
        dataset: DatasetVersion,
        target_name: str,
        target_revision: int,
        adapter: str,
        evaluator_names: tuple[str, ...],
        scenario_overrides: Mapping[str, str],
    ) -> RunResult:
        self.validate(
            target_name=target_name,
            target_revision=target_revision,
            adapter=adapter,
            evaluator_names=evaluator_names,
            scenario_overrides=scenario_overrides,
        )
        kinds = self._evaluator_kinds(evaluator_names)
        with self._tracer.start_as_current_span(
            "evaluation.run",
            kind=SpanKind.INTERNAL,
            record_exception=False,
            set_status_on_exception=False,
        ):
            return await asyncio.to_thread(
                _run_evaluation,
                run_id=run_id,
                dataset=dataset,
                target_name=target_name,
                target_revision=target_revision,
                evaluator_kinds=kinds,
                scenario_overrides=scenario_overrides,
                tracer=self._tracer,
            )

    @staticmethod
    def _evaluator_kinds(
        evaluator_names: tuple[str, ...],
    ) -> tuple[BuiltInEvaluatorKind, ...]:
        try:
            return tuple(BuiltInEvaluatorKind(name) for name in evaluator_names)
        except ValueError as error:
            raise ValueError("unsupported evaluator") from error


def _run_evaluation(
    *,
    run_id: str,
    dataset: DatasetVersion,
    target_name: str,
    target_revision: int,
    evaluator_kinds: tuple[BuiltInEvaluatorKind, ...],
    scenario_overrides: Mapping[str, str],
    tracer: Tracer,
) -> RunResult:
    """Run the synchronous deterministic workload outside the worker event loop."""
    target = _TracedTarget(
        target=DeterministicFakeTarget(
            name=target_name,
            revision=target_revision,
            scenario_overrides=scenario_overrides,
        ),
        tracer=tracer,
    )
    evaluators = tuple(
        _TracedEvaluator(evaluator=evaluator, tracer=tracer)
        for evaluator in build_evaluators(evaluator_kinds)
    )
    return asyncio.run(
        InProcessRunner(clock=DeterministicStepClock()).run(
            run_id=run_id,
            dataset=dataset,
            target=target,
            evaluators=evaluators,
            execution_mode=ExecutionMode.OFFLINE_MOCK,
        )
    )


class _TracedTarget:
    """Trace target execution without retaining any invocation content."""

    __slots__ = ("_target", "_tracer")

    def __init__(self, *, target: TargetPort, tracer: Tracer) -> None:
        self._target = target
        self._tracer = tracer

    @property
    def ref(self) -> ArtifactRef:
        return self._target.ref

    async def invoke(self, request: TargetRequest) -> object:
        with self._tracer.start_as_current_span(
            "evaluation.target.invoke",
            kind=SpanKind.INTERNAL,
            record_exception=False,
            set_status_on_exception=False,
        ):
            return await self._target.invoke(request)


class _TracedEvaluator:
    """Trace evaluator execution without retaining any evaluation content."""

    __slots__ = ("_evaluator", "_tracer")

    def __init__(self, *, evaluator: EvaluatorPort, tracer: Tracer) -> None:
        self._evaluator = evaluator
        self._tracer = tracer

    @property
    def ref(self) -> ArtifactRef:
        return self._evaluator.ref

    @property
    def metric_names(self) -> tuple[MetricName, ...]:
        return self._evaluator.metric_names

    def evaluate(
        self,
        case: EvaluationCase,
        target: TargetObservation,
    ) -> tuple[MetricObservation, ...]:
        with self._tracer.start_as_current_span(
            "evaluation.evaluator.evaluate",
            kind=SpanKind.INTERNAL,
            record_exception=False,
            set_status_on_exception=False,
        ):
            return self._evaluator.evaluate(case, target)


__all__ = ["DeterministicEvaluationExecutor"]
