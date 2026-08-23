"""Explicit offline execution adapter used by the Phase 4 HTTP service."""

from __future__ import annotations

from collections.abc import Mapping

from llm_eval_control_plane.adapters.fake_target import (
    DeterministicFakeTarget,
    DeterministicStepClock,
)
from llm_eval_control_plane.adapters.scorers import (
    BuiltInEvaluatorKind,
    build_evaluators,
)
from llm_eval_control_plane.application.control_plane import ExecutionContract
from llm_eval_control_plane.application.runner import InProcessRunner
from llm_eval_control_plane.domain.datasets import DatasetVersion
from llm_eval_control_plane.domain.results import ExecutionMode, RunResult


class DeterministicEvaluationExecutor:
    """Run only the credential-free deterministic adapter exposed by API v1."""

    _ADAPTER = "deterministic_fake"

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
        return await InProcessRunner(clock=DeterministicStepClock()).run(
            run_id=run_id,
            dataset=dataset,
            target=DeterministicFakeTarget(
                name=target_name,
                revision=target_revision,
                scenario_overrides=scenario_overrides,
            ),
            evaluators=build_evaluators(kinds),
            execution_mode=ExecutionMode.OFFLINE_MOCK,
        )

    @staticmethod
    def _evaluator_kinds(
        evaluator_names: tuple[str, ...],
    ) -> tuple[BuiltInEvaluatorKind, ...]:
        try:
            return tuple(BuiltInEvaluatorKind(name) for name in evaluator_names)
        except ValueError as error:
            raise ValueError("unsupported evaluator") from error


__all__ = ["DeterministicEvaluationExecutor"]
