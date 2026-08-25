from __future__ import annotations

import asyncio
from collections.abc import Mapping
from threading import Event, get_ident

from pytest import MonkeyPatch

from llm_eval_control_plane.adapters.scorers import BuiltInEvaluatorKind
from llm_eval_control_plane.api import execution
from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.domain import (
    CanonicalJson,
    DatasetVersion,
    EvaluationCase,
    RunResult,
)


def _dataset() -> DatasetVersion:
    return DatasetVersion.create(
        name="execution/offload",
        revision=1,
        cases=(
            EvaluationCase(
                case_id="case-001",
                input=CanonicalJson.from_value(
                    {"scenario": "echo", "value": "private-execution-sentinel"}
                ),
                expected=CanonicalJson.from_value("private-execution-sentinel"),
            ),
        ),
    )


def test_deterministic_execution_keeps_the_calling_event_loop_responsive(
    monkeypatch: MonkeyPatch,
) -> None:
    original = execution._run_evaluation
    started = Event()
    release = Event()
    execution_thread: int | None = None

    def blocking_run_evaluation(
        *,
        run_id: str,
        dataset: DatasetVersion,
        target_name: str,
        target_revision: int,
        evaluator_kinds: tuple[BuiltInEvaluatorKind, ...],
        scenario_overrides: Mapping[str, str],
    ) -> RunResult:
        nonlocal execution_thread
        execution_thread = get_ident()
        started.set()
        if not release.wait(timeout=10):
            raise AssertionError("deterministic execution blocked the calling loop")
        return original(
            run_id=run_id,
            dataset=dataset,
            target_name=target_name,
            target_revision=target_revision,
            evaluator_kinds=evaluator_kinds,
            scenario_overrides=scenario_overrides,
        )

    monkeypatch.setattr(execution, "_run_evaluation", blocking_run_evaluation)

    async def exercise() -> tuple[int, RunResult]:
        calling_thread = get_ident()
        task = asyncio.create_task(
            DeterministicEvaluationExecutor().execute(
                run_id="run-offload-001",
                dataset=_dataset(),
                target_name="fake/offload",
                target_revision=1,
                adapter="deterministic_fake",
                evaluator_names=("exact_match",),
                scenario_overrides={},
            )
        )
        progressed = asyncio.Event()
        try:
            assert await asyncio.to_thread(started.wait, 10)
            assert not task.done()
            asyncio.get_running_loop().call_soon(progressed.set)
            await asyncio.wait_for(progressed.wait(), timeout=10)
            assert not task.done()
        finally:
            release.set()
        return calling_thread, await asyncio.wait_for(task, timeout=10)

    calling_thread, result = asyncio.run(exercise())

    assert execution_thread is not None
    assert execution_thread != calling_thread
    assert result.run_id == "run-offload-001"
    assert result.dataset == _dataset().artifact_ref
