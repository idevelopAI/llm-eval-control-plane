import asyncio
from collections.abc import Iterator

from pytest import mark, raises

from llm_eval_control_plane.adapters import (
    BuiltInEvaluatorKind,
    DeterministicFakeTarget,
    build_evaluators,
)
from llm_eval_control_plane.application import InProcessRunner, RunnerConfigurationError
from llm_eval_control_plane.domain import (
    ArtifactKind,
    ArtifactRef,
    CanonicalJson,
    CaseResultStatus,
    DatasetVersion,
    EvaluationCase,
    ExecutionFailure,
    FailureCode,
    RunResult,
    RunStatus,
    ScoredObservation,
    TargetObservation,
    sha256_digest,
)
from llm_eval_control_plane.domain.evaluation import MetricName
from llm_eval_control_plane.domain.execution import MetricObservation


class SequenceClock:
    def __init__(self, values: tuple[float, ...]) -> None:
        self._values: Iterator[float] = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def case(case_id: str, scenario: str, **values: object) -> EvaluationCase:
    expected = values.pop("expected", values.get("value", "answer"))
    return EvaluationCase(
        case_id=case_id,
        input=CanonicalJson.from_value({"scenario": scenario, **values}),
        expected=CanonicalJson.from_value(expected),
    )


def dataset(*cases: EvaluationCase) -> DatasetVersion:
    return DatasetVersion.create(name="fixture", revision=1, cases=tuple(cases))


def execute(
    *,
    dataset_version: DatasetVersion,
    target: object | None = None,
    evaluators: object | None = None,
    clock: SequenceClock | None = None,
    run_id: str = "test-run",
) -> RunResult:
    selected_target = DeterministicFakeTarget() if target is None else target
    selected_evaluators = (
        build_evaluators(
            (
                BuiltInEvaluatorKind.EXACT_MATCH,
                BuiltInEvaluatorKind.LATENCY,
            )
        )
        if evaluators is None
        else evaluators
    )
    runner = InProcessRunner(clock=clock or SequenceClock((0.0, 0.005) * 100))
    return asyncio.run(
        runner.run(
            run_id=run_id,
            dataset=dataset_version,
            target=selected_target,  # type: ignore[arg-type]
            evaluators=selected_evaluators,  # type: ignore[arg-type]
        )
    )


def test_runner_executes_sorted_cases_once_and_aggregates_metrics() -> None:
    target = DeterministicFakeTarget()
    result = execute(
        dataset_version=dataset(
            case("case-b", "mismatch", actual="wrong", expected="answer"),
            case("case-a", "echo", value="answer"),
        ),
        target=target,
        clock=SequenceClock((0.0, 0.005, 1.0, 1.010)),
    )

    assert result.status is RunStatus.COMPLETED
    assert [item.case_id for item in result.cases] == ["case-a", "case-b"]
    assert target.invocations == ("case-a", "case-b")
    summaries = {summary.metric: summary for summary in result.metrics}
    assert summaries["quality.exact_match"].mean == 0.5
    assert summaries["quality.exact_match"].scored == 2
    assert summaries["performance.latency_ms"].mean == 7.5
    assert result.result_digest.startswith("sha256:")


@mark.parametrize("scenario", ["malformed", "missing_usage"])
def test_runner_persists_sanitized_invalid_target_output_and_continues(
    scenario: str,
) -> None:
    result = execute(
        dataset_version=dataset(
            case("case-a", scenario),
            case("case-b", "echo", value="answer"),
        ),
        clock=SequenceClock((0.0, 0.001, 1.0, 1.001)),
    )

    failed = result.cases[0]
    assert failed.status is CaseResultStatus.TARGET_FAILED
    assert failed.target_failure is not None
    assert failed.target_failure.code is FailureCode.INVALID_TARGET_OUTPUT
    assert failed.target_failure.latency_ms == 1.0
    assert "private-sentinel" not in failed.model_dump_json()
    assert result.cases[1].status is CaseResultStatus.COMPLETED
    assert all(summary.errors == 1 for summary in result.metrics)


def test_runner_sanitizes_target_exceptions_and_keeps_elapsed_time() -> None:
    result = execute(
        dataset_version=dataset(
            case("case-a", "raise"),
            case("case-b", "echo", value="answer"),
        ),
        clock=SequenceClock((0.0, 0.002, 1.0, 1.003)),
    )

    failure = result.cases[0].target_failure
    assert failure is not None
    assert failure.code is FailureCode.TARGET_EXCEPTION
    assert failure.message == "Target raised an exception"
    assert failure.latency_ms == 2.0
    assert result.status is RunStatus.COMPLETED_WITH_FAILURES


class BrokenEvaluator:
    def __init__(
        self,
        mode: str,
        *,
        name: str = "broken",
        metric: str = "quality.broken",
        metrics: tuple[str, ...] | None = None,
    ) -> None:
        self.mode = mode
        self.metrics = (metric,) if metrics is None else metrics
        self._ref = ArtifactRef(
            kind=ArtifactKind.EVALUATOR,
            name=name,
            revision=1,
            digest=sha256_digest({"mode": mode, "name": name}),
        )

    @property
    def ref(self) -> ArtifactRef:
        return self._ref

    @property
    def metric_names(self) -> tuple[MetricName, ...]:
        return self.metrics

    def evaluate(
        self, case: EvaluationCase, target: TargetObservation
    ) -> tuple[MetricObservation, ...]:
        del case, target
        if self.mode == "raise":
            raise RuntimeError("private-sentinel")
        if self.mode == "wrong_metric":
            return (
                ScoredObservation(
                    metric="quality.wrong",
                    evaluator=self.ref,
                    value=1.0,
                    reason_code="observed",
                ),
            )
        if self.mode == "wrong_ref":
            return (
                ScoredObservation(
                    metric=self.metric_names[0],
                    evaluator=ArtifactRef(
                        kind=ArtifactKind.EVALUATOR,
                        name="different",
                        revision=1,
                        digest=sha256_digest({"different": True}),
                    ),
                    value=1.0,
                    reason_code="observed",
                ),
            )
        return ("invalid",)  # type: ignore[return-value]


@mark.parametrize(
    ("mode", "expected_code"),
    [
        ("raise", FailureCode.EVALUATOR_EXCEPTION),
        ("wrong_metric", FailureCode.INVALID_EVALUATOR_OUTPUT),
        ("wrong_ref", FailureCode.INVALID_EVALUATOR_OUTPUT),
        ("invalid", FailureCode.INVALID_EVALUATOR_OUTPUT),
    ],
)
def test_runner_sanitizes_evaluator_failures(
    mode: str, expected_code: FailureCode
) -> None:
    good = build_evaluators((BuiltInEvaluatorKind.EXACT_MATCH,))[0]
    result = execute(
        dataset_version=dataset(case("case-a", "echo", value="answer")),
        evaluators=(BrokenEvaluator(mode), good),
        clock=SequenceClock((0.0, 0.001)),
    )

    item = result.cases[0]
    assert item.status is CaseResultStatus.COMPLETED_WITH_ERRORS
    assert len(item.evaluator_failures) == 1
    failure: ExecutionFailure = item.evaluator_failures[0]
    assert failure.code is expected_code
    assert "private-sentinel" not in failure.model_dump_json()
    summaries = {summary.metric: summary for summary in result.metrics}
    assert summaries["quality.broken"].errors == 1
    assert summaries["quality.exact_match"].mean == 1.0


class InvalidPlanTarget(DeterministicFakeTarget):
    def __init__(self, ref: ArtifactRef) -> None:
        super().__init__()
        self._ref = ref


def invalid_ref(kind: ArtifactKind, *, resolved: bool = True) -> ArtifactRef:
    return ArtifactRef(
        kind=kind,
        name="invalid",
        revision=1,
        digest="sha256:" + "a" * 64 if resolved else None,
    )


@mark.parametrize(
    "target_ref",
    [
        invalid_ref(ArtifactKind.DATASET),
        invalid_ref(ArtifactKind.TARGET, resolved=False),
    ],
)
def test_runner_rejects_unresolved_or_wrong_target_plan(
    target_ref: ArtifactRef,
) -> None:
    with raises(RunnerConfigurationError, match="resolved target"):
        execute(
            dataset_version=dataset(case("case-a", "echo", value="answer")),
            target=InvalidPlanTarget(target_ref),
        )


def test_runner_rejects_missing_duplicate_and_overlapping_evaluators() -> None:
    fixture = dataset(case("case-a", "echo", value="answer"))
    exact = build_evaluators((BuiltInEvaluatorKind.EXACT_MATCH,))[0]

    with raises(RunnerConfigurationError, match="at least one"):
        execute(dataset_version=fixture, evaluators=())
    with raises(RunnerConfigurationError, match="references must be unique"):
        execute(dataset_version=fixture, evaluators=(exact, exact))
    with raises(RunnerConfigurationError, match="metric names must be unique"):
        execute(
            dataset_version=fixture,
            evaluators=(
                exact,
                BrokenEvaluator(
                    "raise",
                    name="another",
                    metric="quality.exact_match",
                ),
            ),
        )


def test_runner_rejects_invalid_evaluator_identity_and_metric_declarations() -> None:
    fixture = dataset(case("case-a", "echo", value="answer"))
    wrong_kind = BrokenEvaluator("raise")
    wrong_kind._ref = invalid_ref(ArtifactKind.TARGET)
    unresolved = BrokenEvaluator("raise", name="unresolved")
    unresolved._ref = invalid_ref(ArtifactKind.EVALUATOR, resolved=False)

    for evaluator in (wrong_kind, unresolved):
        with raises(RunnerConfigurationError, match="resolved evaluator"):
            execute(dataset_version=fixture, evaluators=(evaluator,))
    with raises(RunnerConfigurationError, match="declare metrics"):
        execute(
            dataset_version=fixture,
            evaluators=(BrokenEvaluator("raise", metrics=()),),
        )
    with raises(RunnerConfigurationError, match="metrics must be unique"):
        execute(
            dataset_version=fixture,
            evaluators=(
                BrokenEvaluator(
                    "raise",
                    metrics=("quality.one", "quality.one"),
                ),
            ),
        )


def test_runner_counts_skipped_and_error_observations_explicitly() -> None:
    fixture = dataset(case("case-a", "echo", value=1, expected=1))
    evaluators = build_evaluators(
        (
            BuiltInEvaluatorKind.NORMALIZED_MATCH,
            BuiltInEvaluatorKind.NUMERIC_TOLERANCE,
        )
    )

    result = execute(
        dataset_version=fixture,
        evaluators=evaluators,
        clock=SequenceClock((0.0, 0.001)),
    )

    summaries = {summary.metric: summary for summary in result.metrics}
    assert summaries["quality.normalized_match"].errors == 1
    assert summaries["quality.numeric_within_tolerance"].skipped == 1
    assert result.status is RunStatus.COMPLETED_WITH_FAILURES


def test_runner_rejects_invalid_monotonic_clock() -> None:
    with raises(RunnerConfigurationError, match="monotonic clock"):
        execute(
            dataset_version=dataset(case("case-a", "echo", value="answer")),
            clock=SequenceClock((2.0, 1.0)),
        )


def test_run_content_is_reproducible_independently_from_run_id() -> None:
    fixture = dataset(case("case-a", "echo", value="answer"))
    first = execute(
        dataset_version=fixture,
        run_id="first",
        clock=SequenceClock((0.0, 0.005)),
    )
    second = execute(
        dataset_version=fixture,
        run_id="second",
        clock=SequenceClock((10.0, 10.005)),
    )

    assert first.run_id != second.run_id
    assert first.result_digest == second.result_digest
    assert first.cases == second.cases
