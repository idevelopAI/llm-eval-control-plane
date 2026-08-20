import asyncio
from collections.abc import Iterator, Mapping

from pytest import approx, raises

from llm_eval_control_plane.adapters import BuiltInEvaluatorKind, build_evaluators
from llm_eval_control_plane.application import (
    ComparisonConfigurationError,
    InProcessRunner,
    compare_runs,
)
from llm_eval_control_plane.domain import (
    ArtifactKind,
    ArtifactRef,
    CanonicalJson,
    CaseChange,
    DatasetVersion,
    EvaluationCase,
    EvaluationSpec,
    ExecutionMode,
    GateStatus,
    MetricDirection,
    MetricGate,
    ReleaseStatus,
    RunResult,
    TargetOutcome,
    TargetRequest,
    TargetResponse,
    TokenUsage,
    sha256_digest,
)


class SequenceClock:
    def __init__(self, values: tuple[float, ...]) -> None:
        self._values: Iterator[float] = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class MappingTarget:
    def __init__(
        self,
        revision: int,
        responses: Mapping[str, tuple[object, TargetOutcome]],
        *,
        failing_case: str | None = None,
    ) -> None:
        self._responses = responses
        self._failing_case = failing_case
        self._ref = ArtifactRef(
            kind=ArtifactKind.TARGET,
            name="fake/release",
            revision=revision,
            digest=sha256_digest(
                {
                    "failing_case": failing_case,
                    "responses": {
                        key: [value, outcome.value]
                        for key, (value, outcome) in sorted(responses.items())
                    },
                }
            ),
        )

    @property
    def ref(self) -> ArtifactRef:
        return self._ref

    async def invoke(self, request: TargetRequest) -> object:
        if request.case_id == self._failing_case:
            raise RuntimeError("private-sentinel")
        output, outcome = self._responses[request.case_id]
        return TargetResponse(
            output=CanonicalJson.from_value(output),
            outcome=outcome,
            refusal_code=("policy_block" if outcome is TargetOutcome.REFUSED else None),
            usage=TokenUsage(input_units=1, output_units=1),
        )


def fixture_dataset() -> DatasetVersion:
    return DatasetVersion.create(
        name="release-gate/fixture",
        revision=1,
        cases=(
            EvaluationCase(
                case_id="quality-de",
                input=CanonicalJson.from_value({"prompt": "de"}),
                expected=CanonicalJson.from_value("good-de"),
                slices=(
                    "answerability/answerable",
                    "language/de",
                    "safety/safe",
                    "task/qa",
                ),
            ),
            EvaluationCase(
                case_id="quality-en",
                input=CanonicalJson.from_value({"prompt": "en"}),
                expected=CanonicalJson.from_value("good-en"),
                slices=(
                    "answerability/answerable",
                    "language/en",
                    "safety/safe",
                    "task/qa",
                ),
            ),
            EvaluationCase(
                case_id="refusal-en",
                input=CanonicalJson.from_value({"prompt": "unsafe"}),
                expected=CanonicalJson.from_value("refused"),
                expected_refusal=True,
                slices=(
                    "answerability/unanswerable",
                    "language/en",
                    "safety/refusal",
                    "task/refusal",
                ),
            ),
        ),
    )


BASELINE_RESPONSES = {
    "quality-de": ("good-de", TargetOutcome.COMPLETED),
    "quality-en": ("good-en", TargetOutcome.COMPLETED),
    "refusal-en": ("refused", TargetOutcome.REFUSED),
}
CANDIDATE_RESPONSES = {
    **BASELINE_RESPONSES,
    "quality-en": ("wrong", TargetOutcome.COMPLETED),
    "refusal-en": ("refused", TargetOutcome.COMPLETED),
}


def execute(
    *,
    revision: int,
    responses: Mapping[str, tuple[object, TargetOutcome]],
    run_id: str,
    failing_case: str | None = None,
) -> RunResult:
    return asyncio.run(
        InProcessRunner(clock=SequenceClock((0.0, 0.005, 1.0, 1.005, 2.0, 2.005))).run(
            run_id=run_id,
            dataset=fixture_dataset(),
            target=MappingTarget(
                revision,
                responses,
                failing_case=failing_case,
            ),
            evaluators=build_evaluators(
                (
                    BuiltInEvaluatorKind.EXACT_MATCH,
                    BuiltInEvaluatorKind.REFUSAL,
                    BuiltInEvaluatorKind.LATENCY,
                )
            ),
        )
    )


def policy(
    baseline: RunResult,
    candidate: RunResult,
    *,
    gates: tuple[MetricGate, ...] | None = None,
) -> EvaluationSpec:
    return EvaluationSpec(
        name="release-policy",
        dataset=fixture_dataset().artifact_ref,
        baseline=baseline.target.model_copy(update={"digest": None}),
        candidate=candidate.target.model_copy(update={"digest": None}),
        gates=gates
        or (
            MetricGate(
                metric="quality.exact_match",
                direction=MetricDirection.HIGHER_IS_BETTER,
                threshold=0.6,
                allowed_regression=0.34,
            ),
            MetricGate(
                metric="safety.refusal_correct",
                slice="safety/refusal",
                direction=MetricDirection.HIGHER_IS_BETTER,
                threshold=1.0,
                allowed_regression=0.0,
            ),
            MetricGate(
                metric="performance.latency_ms",
                direction=MetricDirection.LOWER_IS_BETTER,
                threshold=5.0,
                allowed_regression=0.0,
            ),
        ),
    )


def compared(*, regressed: bool = True) -> tuple[RunResult, RunResult]:
    baseline = execute(
        revision=1,
        responses=BASELINE_RESPONSES,
        run_id="baseline",
    )
    candidate = execute(
        revision=2,
        responses=CANDIDATE_RESPONSES if regressed else BASELINE_RESPONSES,
        run_id="candidate",
    )
    return baseline, candidate


def test_compare_runs_fails_safety_independently_of_passing_quality() -> None:
    baseline, candidate = compared()

    decision = compare_runs(
        spec=policy(baseline, candidate),
        dataset=fixture_dataset(),
        baseline=baseline,
        candidate=candidate,
    )

    gates = {(gate.metric, gate.slice): gate for gate in decision.gates}
    assert decision.status is ReleaseStatus.FAILED
    assert gates[("quality.exact_match", None)].status is GateStatus.PASSED
    assert gates[("performance.latency_ms", None)].status is GateStatus.PASSED
    assert (
        gates[("safety.refusal_correct", "safety/refusal")].status is GateStatus.FAILED
    )
    assert gates[("quality.exact_match", None)].aggregate.delta == approx(-(1 / 3))

    changes = {
        (item.metric, item.slice, item.case_id): item.change for item in decision.cases
    }
    assert changes[("quality.exact_match", None, "quality-en")] is (
        CaseChange.NEWLY_FAILING
    )
    assert (
        changes[("safety.refusal_correct", "safety/refusal", "refusal-en")]
        is CaseChange.NEWLY_FAILING
    )
    assert any(
        aggregate.slice == "language/en"
        and aggregate.metric == "quality.exact_match"
        and aggregate.candidate.mean == 0.5
        for aggregate in decision.aggregates
    )


def test_identical_evidence_produces_zero_deltas_and_passes() -> None:
    baseline, candidate = compared(regressed=False)

    decision = compare_runs(
        spec=policy(baseline, candidate),
        dataset=fixture_dataset(),
        baseline=baseline,
        candidate=candidate,
    )

    assert decision.status is ReleaseStatus.PASSED
    assert all(item.status is GateStatus.PASSED for item in decision.gates)
    assert all(item.aggregate.delta == 0.0 for item in decision.gates)


def test_gate_boundary_tolerates_only_machine_precision_noise() -> None:
    baseline, candidate = compared()
    exact_boundary = policy(
        baseline,
        candidate,
        gates=(
            MetricGate(
                metric="quality.exact_match",
                direction=MetricDirection.HIGHER_IS_BETTER,
                threshold=2 / 3,
                allowed_regression=1 / 3,
            ),
        ),
    )

    decision = compare_runs(
        spec=exact_boundary,
        dataset=fixture_dataset(),
        baseline=baseline,
        candidate=candidate,
    )

    gate = decision.gates[0]
    assert gate.status is GateStatus.PASSED
    assert gate.threshold_passed is True
    assert gate.regression_passed is True


def test_target_failure_becomes_a_coverage_gate_failure() -> None:
    baseline, _candidate = compared(regressed=False)
    candidate = execute(
        revision=2,
        responses=BASELINE_RESPONSES,
        run_id="candidate-failed",
        failing_case="quality-en",
    )

    decision = compare_runs(
        spec=policy(baseline, candidate),
        dataset=fixture_dataset(),
        baseline=baseline,
        candidate=candidate,
    )

    exact_gate = next(
        item for item in decision.gates if item.metric == "quality.exact_match"
    )
    assert exact_gate.status is GateStatus.FAILED
    assert exact_gate.coverage_passed is False
    assert "private-sentinel" not in decision.model_dump_json()


def test_compare_runs_rejects_invalid_policy_and_evidence_alignment() -> None:
    baseline, candidate = compared(regressed=False)
    no_baseline = policy(baseline, candidate).model_copy(update={"baseline": None})
    with raises(ComparisonConfigurationError, match="requires a baseline"):
        compare_runs(
            spec=no_baseline,
            dataset=fixture_dataset(),
            baseline=baseline,
            candidate=candidate,
        )

    wrong_slice = policy(
        baseline,
        candidate,
        gates=(
            MetricGate(
                metric="quality.exact_match",
                slice="language/fr",
                direction=MetricDirection.HIGHER_IS_BETTER,
                threshold=1.0,
            ),
        ),
    )
    with raises(ComparisonConfigurationError, match="metric or slice"):
        compare_runs(
            spec=wrong_slice,
            dataset=fixture_dataset(),
            baseline=baseline,
            candidate=candidate,
        )

    wrong_target = policy(baseline, candidate).model_copy(
        update={"candidate": candidate.target.model_copy(update={"name": "other"})}
    )
    with raises(ComparisonConfigurationError, match="candidate target"):
        compare_runs(
            spec=wrong_target,
            dataset=fixture_dataset(),
            baseline=baseline,
            candidate=candidate,
        )

    live_candidate = RunResult.create(
        run_id=candidate.run_id,
        dataset=candidate.dataset,
        target=candidate.target,
        evaluators=candidate.evaluators,
        cases=candidate.cases,
        metrics=candidate.metrics,
        execution_mode=ExecutionMode.LIVE,
    )
    with raises(ComparisonConfigurationError, match="execution modes must match"):
        compare_runs(
            spec=policy(baseline, live_candidate),
            dataset=fixture_dataset(),
            baseline=baseline,
            candidate=live_candidate,
        )


def test_compare_runs_recomputes_and_verifies_stored_aggregates() -> None:
    baseline, candidate = compared(regressed=False)
    metrics = list(baseline.metrics)
    metrics[0] = metrics[0].model_copy(update={"mean": 0.0})
    inconsistent = RunResult.create(
        run_id=baseline.run_id,
        dataset=baseline.dataset,
        target=baseline.target,
        evaluators=baseline.evaluators,
        cases=baseline.cases,
        metrics=tuple(metrics),
    )

    with raises(ComparisonConfigurationError, match="do not match case evidence"):
        compare_runs(
            spec=policy(inconsistent, candidate),
            dataset=fixture_dataset(),
            baseline=inconsistent,
            candidate=candidate,
        )
