from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from threading import Event, get_ident

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode, Tracer
from pytest import MonkeyPatch, mark, raises

from llm_eval_control_plane.adapters.scorers import (
    BuiltInEvaluatorKind,
    build_evaluators,
)
from llm_eval_control_plane.api import execution
from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.application import EvaluatorPort
from llm_eval_control_plane.domain import (
    ArtifactRef,
    CanonicalJson,
    DatasetVersion,
    EvaluationCase,
    EvaluationSuiteVersion,
    ExecutionMode,
    MetricDirection,
    MetricGate,
    RunResult,
    TargetObservation,
)
from llm_eval_control_plane.domain.evaluation import MetricName
from llm_eval_control_plane.domain.execution import MetricObservation


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


def _suite() -> EvaluationSuiteVersion:
    contract = DeterministicEvaluationExecutor().validate_suite(
        adapter="deterministic_fake",
        evaluator_names=("exact_match",),
    )
    return EvaluationSuiteVersion.create(
        name="execution/protocol",
        revision=1,
        dataset=_dataset().artifact_ref,
        evaluators=contract.evaluators,
        slices=(),
        execution=contract.execution,
        gates=(
            MetricGate(
                metric="quality.exact_match",
                direction=MetricDirection.HIGHER_IS_BETTER,
                threshold=1.0,
            ),
        ),
    )


def test_suite_execution_pins_provenance_and_keeps_trace_content_private() -> None:
    provider, exporter = _tracing()
    executor = DeterministicEvaluationExecutor(tracer=provider.get_tracer("suite-test"))
    suite = _suite()

    result = asyncio.run(
        executor.execute_suite(
            run_id="private-suite-run-sentinel",
            dataset=_dataset(),
            target_name="private-suite-target-sentinel",
            target_revision=2,
            scenario_overrides={},
            suite=suite,
        )
    )

    assert result.suite == suite.artifact_ref
    assert result.dataset == suite.dataset
    assert result.evaluators == suite.evaluator_refs
    assert result.execution_mode is suite.execution.execution_mode
    spans = tuple(exporter.get_finished_spans())
    assert [span.name for span in spans] == [
        "evaluation.target.invoke",
        "evaluation.evaluator.evaluate",
        "evaluation.run",
    ]
    _assert_content_free_internal_spans(spans)
    captured = _span_capture(spans)
    for private_value in (
        suite.name,
        suite.digest,
        "private-suite-run-sentinel",
        "private-suite-target-sentinel",
        "private-execution-sentinel",
    ):
        assert private_value not in captured
    provider.shutdown()


@mark.parametrize("dependency", ["dataset", "settings", "evaluator"])
def test_suite_execution_rejects_dependency_drift_before_invoking_target(
    dependency: str,
    monkeypatch: MonkeyPatch,
) -> None:
    suite = _suite()
    dataset = _dataset()
    if dependency == "dataset":
        dataset = DatasetVersion.create(
            name=dataset.name, revision=2, cases=dataset.cases
        )
    elif dependency == "settings":
        suite = EvaluationSuiteVersion.create(
            name=suite.name,
            revision=2,
            dataset=suite.dataset,
            evaluators=suite.evaluators,
            slices=suite.slices,
            execution=suite.execution.model_copy(
                update={"execution_mode": ExecutionMode.LIVE}
            ),
            gates=suite.gates,
        )
    else:
        evaluator = suite.evaluators[0]
        suite = EvaluationSuiteVersion.create(
            name=suite.name,
            revision=2,
            dataset=suite.dataset,
            evaluators=(
                evaluator.model_copy(
                    update={
                        "artifact": evaluator.artifact.model_copy(
                            update={"revision": 2}
                        )
                    }
                ),
            ),
            slices=suite.slices,
            execution=suite.execution,
            gates=suite.gates,
        )
    calls = 0

    def reject_execution(**_kwargs: object) -> RunResult:
        nonlocal calls
        calls += 1
        raise AssertionError("preflight validation must prevent target execution")

    monkeypatch.setattr(execution, "_run_evaluation", reject_execution)

    with raises(ValueError, match="suite execution dependencies do not match"):
        asyncio.run(
            DeterministicEvaluationExecutor().execute_suite(
                run_id="suite-invalid",
                dataset=dataset,
                target_name="fake/suite-invalid",
                target_revision=1,
                scenario_overrides={},
                suite=suite,
            )
        )

    assert calls == 0


def test_suite_validation_resolves_exact_execution_and_evaluator_behavior() -> None:
    executor = DeterministicEvaluationExecutor()

    contract = executor.validate_suite(
        adapter="deterministic_fake",
        evaluator_names=("exact_match", "usage"),
    )

    assert contract.execution.adapter == "deterministic_fake"
    assert contract.execution.execution_mode is ExecutionMode.OFFLINE_MOCK
    assert contract.execution.invocations_per_case == 1
    assert contract.execution.max_concurrency == 1
    assert contract.evaluator_names == ("exact_match", "usage")
    resolved = build_evaluators(
        (BuiltInEvaluatorKind.EXACT_MATCH, BuiltInEvaluatorKind.USAGE)
    )
    assert tuple(item.artifact for item in contract.evaluators) == tuple(
        item.ref for item in resolved
    )
    assert tuple(item.metrics for item in contract.evaluators) == tuple(
        item.metric_names for item in resolved
    )

    with raises(ValueError, match="unsupported target adapter"):
        executor.validate_suite(
            adapter="unsupported",
            evaluator_names=("exact_match",),
        )
    with raises(ValueError, match="unsupported evaluator"):
        executor.validate_suite(
            adapter="deterministic_fake",
            evaluator_names=("unsupported",),
        )


@mark.parametrize("suite_backed", [False, True])
def test_deterministic_execution_keeps_the_calling_event_loop_responsive(
    monkeypatch: MonkeyPatch,
    suite_backed: bool,
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
        tracer: Tracer,
        suite: EvaluationSuiteVersion | None = None,
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
            tracer=tracer,
            suite=suite,
        )

    monkeypatch.setattr(execution, "_run_evaluation", blocking_run_evaluation)

    async def exercise() -> tuple[int, RunResult]:
        calling_thread = get_ident()
        executor = DeterministicEvaluationExecutor()
        operation = (
            executor.execute_suite(
                run_id="run-offload-001",
                dataset=_dataset(),
                target_name="fake/offload",
                target_revision=1,
                scenario_overrides={},
                suite=_suite(),
            )
            if suite_backed
            else executor.execute(
                run_id="run-offload-001",
                dataset=_dataset(),
                target_name="fake/offload",
                target_revision=1,
                adapter="deterministic_fake",
                evaluator_names=("exact_match",),
                scenario_overrides={},
            )
        )
        task = asyncio.create_task(operation)
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


def _tracing() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _span_capture(spans: tuple[ReadableSpan, ...]) -> str:
    return json.dumps(
        [
            {
                "attributes": dict(span.attributes or {}),
                "events": [
                    {
                        "attributes": dict(event.attributes or {}),
                        "name": event.name,
                    }
                    for event in span.events
                ],
                "links": [dict(link.attributes or {}) for link in span.links],
                "name": span.name,
                "status": span.status.status_code.name,
                "status_description": span.status.description,
            }
            for span in spans
        ],
        sort_keys=True,
    )


def _assert_content_free_internal_spans(spans: tuple[ReadableSpan, ...]) -> None:
    for span in spans:
        assert span.kind is SpanKind.INTERNAL
        assert dict(span.attributes or {}) == {}
        assert span.events == ()
        assert span.links == ()
        assert span.status.status_code is StatusCode.UNSET
        assert span.status.description is None


def test_execution_traces_run_target_and_evaluators_under_active_worker_span() -> None:
    provider, exporter = _tracing()
    tracer = provider.get_tracer("execution-test")
    executor = DeterministicEvaluationExecutor(tracer=tracer)
    contract = executor.validate(
        target_name="private-target-name-sentinel",
        target_revision=1,
        adapter="deterministic_fake",
        evaluator_names=("exact_match", "usage"),
        scenario_overrides={},
    )

    with tracer.start_as_current_span("worker.job.execute") as worker_span:
        result = asyncio.run(
            executor.execute(
                run_id="private-run-id-sentinel",
                dataset=_dataset(),
                target_name="private-target-name-sentinel",
                target_revision=1,
                adapter="deterministic_fake",
                evaluator_names=("exact_match", "usage"),
                scenario_overrides={},
            )
        )

    spans = tuple(exporter.get_finished_spans())
    run_span = next(span for span in spans if span.name == "evaluation.run")
    operation_spans = tuple(
        span for span in spans if span.name.startswith("evaluation.")
    )

    assert result.run_id == "private-run-id-sentinel"
    assert result.target == contract.target
    assert result.evaluators == contract.evaluators
    assert result.execution_mode is contract.execution_mode
    assert [span.name for span in operation_spans] == [
        "evaluation.target.invoke",
        "evaluation.evaluator.evaluate",
        "evaluation.evaluator.evaluate",
        "evaluation.run",
    ]
    worker_context = worker_span.get_span_context()
    assert run_span.parent is not None
    assert run_span.parent.span_id == worker_context.span_id
    assert run_span.context is not None
    for child in operation_spans[:-1]:
        assert child.parent is not None
        assert child.parent.span_id == run_span.context.span_id
    _assert_content_free_internal_spans(operation_spans)

    captured = _span_capture(spans)
    for sentinel in (
        "private-execution-sentinel",
        "private-run-id-sentinel",
        "private-target-name-sentinel",
        "case-001",
        "exact_match",
        "usage",
    ):
        assert sentinel not in captured
    provider.shutdown()


class _RaisingEvaluator:
    def __init__(self, evaluator: EvaluatorPort) -> None:
        self._evaluator = evaluator

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
        del case, target
        raise RuntimeError("private-evaluator-exception-sentinel")


def test_exception_spans_never_record_failure_or_evaluation_content(
    monkeypatch: MonkeyPatch,
) -> None:
    provider, exporter = _tracing()
    tracer = provider.get_tracer("execution-exception-test")

    def raising_evaluators(
        kinds: tuple[BuiltInEvaluatorKind, ...],
    ) -> tuple[EvaluatorPort, ...]:
        return tuple(
            _RaisingEvaluator(evaluator) for evaluator in build_evaluators(kinds)
        )

    monkeypatch.setattr(
        "llm_eval_control_plane.api.execution.build_evaluators",
        raising_evaluators,
    )
    dataset = DatasetVersion.create(
        name="private-dataset-name-sentinel",
        revision=1,
        cases=(
            EvaluationCase(
                case_id="private-target-case-sentinel",
                input=CanonicalJson.from_value(
                    {
                        "scenario": "raise",
                        "value": "private-target-input-sentinel",
                    }
                ),
            ),
            EvaluationCase(
                case_id="private-evaluator-case-sentinel",
                input=CanonicalJson.from_value(
                    {
                        "scenario": "echo",
                        "value": "private-evaluator-input-sentinel",
                    }
                ),
                expected=CanonicalJson.from_value(
                    "private-evaluator-expectation-sentinel"
                ),
            ),
        ),
    )

    result = asyncio.run(
        DeterministicEvaluationExecutor(tracer=tracer).execute(
            run_id="private-exception-run-sentinel",
            dataset=dataset,
            target_name="private-exception-target-sentinel",
            target_revision=1,
            adapter="deterministic_fake",
            evaluator_names=("exact_match",),
            scenario_overrides={},
        )
    )

    spans = tuple(exporter.get_finished_spans())
    run_span = next(span for span in spans if span.name == "evaluation.run")
    operation_spans = tuple(
        span for span in spans if span.name.startswith("evaluation.")
    )
    operation_names = [span.name for span in operation_spans]
    assert operation_names.count("evaluation.target.invoke") == 2
    assert operation_names.count("evaluation.evaluator.evaluate") == 1
    assert operation_names[-1] == "evaluation.run"
    assert run_span.context is not None
    for child in operation_spans[:-1]:
        assert child.parent is not None
        assert child.parent.span_id == run_span.context.span_id
    _assert_content_free_internal_spans(operation_spans)
    cases = {case.case_id: case for case in result.cases}
    assert cases["private-target-case-sentinel"].target_failure is not None
    assert cases["private-evaluator-case-sentinel"].evaluator_failures

    captured = _span_capture(spans)
    for sentinel in (
        "private-dataset-name-sentinel",
        "private-target-case-sentinel",
        "private-evaluator-case-sentinel",
        "private-target-input-sentinel",
        "private-evaluator-input-sentinel",
        "private-evaluator-expectation-sentinel",
        "private-evaluator-exception-sentinel",
        "private-exception-run-sentinel",
        "private-exception-target-sentinel",
        "FakeTargetError",
        "RuntimeError",
        "exact_match",
    ):
        assert sentinel not in captured
    provider.shutdown()
