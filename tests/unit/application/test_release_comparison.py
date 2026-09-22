import asyncio
from collections.abc import Iterator, Mapping

from pytest import approx, mark, raises

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
    CaseResult,
    CaseResultStatus,
    DatasetVersion,
    EvaluationCase,
    EvaluationSpec,
    EvaluationSuiteVersion,
    ExecutionFailure,
    ExecutionMode,
    FailureCode,
    FailureStage,
    GateStatus,
    MetricDirection,
    MetricGate,
    ReleaseStatus,
    RunResult,
    SuiteEvaluator,
    SuiteExecutionSettings,
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
    suite: EvaluationSuiteVersion | None = None,
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
            suite=suite,
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
    assert decision.suite is None


def comparison_suite() -> EvaluationSuiteVersion:
    evaluators = build_evaluators(
        (
            BuiltInEvaluatorKind.EXACT_MATCH,
            BuiltInEvaluatorKind.REFUSAL,
            BuiltInEvaluatorKind.LATENCY,
        )
    )
    return EvaluationSuiteVersion.create(
        name="release-suite",
        revision=1,
        dataset=fixture_dataset().artifact_ref,
        evaluators=tuple(
            SuiteEvaluator(
                executor_name=evaluator.ref.name.removeprefix("builtin/"),
                artifact=evaluator.ref,
                metrics=evaluator.metric_names,
            )
            for evaluator in evaluators
        ),
        slices=("safety/refusal",),
        execution=SuiteExecutionSettings(
            adapter="deterministic_fake",
            execution_mode=ExecutionMode.OFFLINE_DETERMINISTIC_FIXTURE,
        ),
        gates=(
            MetricGate(
                metric="quality.exact_match",
                direction=MetricDirection.HIGHER_IS_BETTER,
                threshold=1.0,
                allowed_regression=0.0,
            ),
        ),
    )


def suite_runs(suite: EvaluationSuiteVersion) -> tuple[RunResult, RunResult]:
    return (
        execute(
            revision=1,
            responses=BASELINE_RESPONSES,
            run_id="suite-baseline",
            suite=suite,
        ),
        execute(
            revision=2,
            responses=CANDIDATE_RESPONSES,
            run_id="suite-candidate",
            suite=suite,
        ),
    )


def test_comparison_pins_suite_and_applies_its_gate_policy() -> None:
    suite = comparison_suite()
    baseline, candidate = suite_runs(suite)

    decision = compare_runs(
        suite=suite,
        spec=suite.to_evaluation_spec(
            baseline=baseline.target, candidate=candidate.target
        ),
        dataset=fixture_dataset(),
        baseline=baseline,
        candidate=candidate,
    )

    assert decision.suite == suite.artifact_ref
    assert decision.status is ReleaseStatus.FAILED
    assert decision.gates[0].threshold == 1.0
    assert decision.baseline_result_digest == baseline.result_digest
    assert decision.candidate_result_digest == candidate.result_digest


def test_comparison_rejects_policy_override_for_suite_pinned_runs() -> None:
    suite = comparison_suite()
    baseline, candidate = suite_runs(suite)
    exact_spec = suite.to_evaluation_spec(
        baseline=baseline.target, candidate=candidate.target
    )
    relaxed_spec = exact_spec.model_copy(
        update={
            "gates": (
                MetricGate(
                    metric="quality.exact_match",
                    direction=MetricDirection.HIGHER_IS_BETTER,
                    threshold=0.0,
                    allowed_regression=1.0,
                ),
            )
        }
    )

    with raises(ComparisonConfigurationError, match="policy must exactly match"):
        compare_runs(
            suite=suite,
            spec=relaxed_spec,
            dataset=fixture_dataset(),
            baseline=baseline,
            candidate=candidate,
        )
    with raises(ComparisonConfigurationError, match=r"require.*suite snapshot"):
        compare_runs(
            spec=relaxed_spec,
            dataset=fixture_dataset(),
            baseline=baseline,
            candidate=candidate,
        )


@mark.parametrize("suite_pin", ["missing", "different_revision", "different_digest"])
def test_comparison_rejects_mixed_or_different_suite_evidence(suite_pin: str) -> None:
    suite = comparison_suite()
    baseline, candidate = suite_runs(suite)
    candidate_pin = None
    if suite_pin == "different_revision":
        candidate_pin = suite.artifact_ref.model_copy(update={"revision": 2})
    elif suite_pin == "different_digest":
        candidate_pin = suite.artifact_ref.model_copy(
            update={"digest": sha256_digest({"different": True})}
        )
    altered = RunResult.create(
        run_id=candidate.run_id,
        dataset=candidate.dataset,
        target=candidate.target,
        evaluators=candidate.evaluators,
        cases=candidate.cases,
        metrics=candidate.metrics,
        suite=candidate_pin,
    )

    with raises(ComparisonConfigurationError, match="must pin the supplied"):
        compare_runs(
            suite=suite,
            spec=suite.to_evaluation_spec(
                baseline=baseline.target, candidate=candidate.target
            ),
            dataset=fixture_dataset(),
            baseline=baseline,
            candidate=altered,
        )


@mark.parametrize("drift", ["evaluators", "metrics", "metric_binding", "mode"])
def test_comparison_validates_evidence_against_suite_contract(drift: str) -> None:
    suite = comparison_suite()
    baseline, candidate = suite_runs(suite)
    altered = RunResult.create(
        run_id=candidate.run_id,
        dataset=candidate.dataset,
        target=candidate.target,
        evaluators=(
            candidate.evaluators[:-1] if drift == "evaluators" else candidate.evaluators
        ),
        cases=candidate.cases,
        metrics=(
            candidate.metrics[:-1]
            if drift == "metrics"
            else tuple(
                summary.model_copy(update={"evaluator": candidate.evaluators[0]})
                for summary in candidate.metrics
            )
            if drift == "metric_binding"
            else candidate.metrics
        ),
        execution_mode=(
            ExecutionMode.LIVE if drift == "mode" else candidate.execution_mode
        ),
        suite=suite.artifact_ref,
    )

    with raises(ComparisonConfigurationError, match=r"does not match|do not match"):
        compare_runs(
            suite=suite,
            spec=suite.to_evaluation_spec(
                baseline=baseline.target, candidate=candidate.target
            ),
            dataset=fixture_dataset(),
            baseline=baseline,
            candidate=altered,
        )


@mark.parametrize("side", ["baseline", "candidate"])
@mark.parametrize("drift", ["revision", "digest", "metric", "binding"])
def test_suite_comparison_rejects_observations_outside_declared_binding(
    side: str, drift: str
) -> None:
    suite = comparison_suite()
    baseline, candidate = suite_runs(suite)
    original = baseline if side == "baseline" else candidate
    case = original.cases[0]
    observation = case.observations[0]
    if drift == "metric":
        changed = observation.model_copy(update={"metric": "quality.undeclared"})
    else:
        evaluator = observation.evaluator
        if drift == "revision":
            evaluator = evaluator.model_copy(update={"revision": 999})
        elif drift == "digest":
            evaluator = evaluator.model_copy(
                update={"digest": sha256_digest("different evaluator behavior")}
            )
        else:
            evaluator = next(item for item in suite.evaluator_refs if item != evaluator)
        changed = observation.model_copy(update={"evaluator": evaluator})
    changed_case = CaseResult.model_validate(
        {
            **case.model_dump(mode="python"),
            "observations": tuple(
                sorted(
                    (changed, *case.observations[1:]),
                    key=lambda item: (item.evaluator.logical_key, item.metric),
                )
            ),
        }
    )
    altered = RunResult.create(
        run_id=original.run_id,
        dataset=original.dataset,
        target=original.target,
        evaluators=original.evaluators,
        cases=(changed_case, *original.cases[1:]),
        metrics=original.metrics,
        execution_mode=original.execution_mode,
        suite=suite.artifact_ref,
    )

    with raises(ComparisonConfigurationError, match="observations do not match"):
        compare_runs(
            suite=suite,
            spec=suite.to_evaluation_spec(
                baseline=baseline.target, candidate=candidate.target
            ),
            dataset=fixture_dataset(),
            baseline=altered if side == "baseline" else baseline,
            candidate=altered if side == "candidate" else candidate,
        )


@mark.parametrize("side", ["baseline", "candidate"])
@mark.parametrize("drift", ["revision", "digest", "unresolved"])
def test_suite_comparison_rejects_failures_from_undeclared_evaluators(
    side: str, drift: str
) -> None:
    suite = comparison_suite()
    baseline, candidate = suite_runs(suite)
    original = baseline if side == "baseline" else candidate
    evaluator = suite.evaluator_refs[0]
    if drift == "revision":
        evaluator = evaluator.model_copy(update={"revision": 999})
    else:
        evaluator = evaluator.model_copy(
            update={
                "digest": None
                if drift == "unresolved"
                else sha256_digest("different evaluator behavior")
            }
        )
    case = original.cases[0]
    changed_case = CaseResult.model_validate(
        {
            **case.model_dump(mode="python"),
            "status": CaseResultStatus.COMPLETED_WITH_ERRORS,
            "evaluator_failures": (
                ExecutionFailure(
                    stage=FailureStage.EVALUATOR,
                    code=FailureCode.EVALUATOR_EXCEPTION,
                    message="Evaluator failed",
                    evaluator=evaluator,
                ),
            ),
        }
    )
    altered = RunResult.create(
        run_id=original.run_id,
        dataset=original.dataset,
        target=original.target,
        evaluators=original.evaluators,
        cases=(changed_case, *original.cases[1:]),
        metrics=original.metrics,
        execution_mode=original.execution_mode,
        suite=suite.artifact_ref,
    )

    with raises(ComparisonConfigurationError, match="failures do not match"):
        compare_runs(
            suite=suite,
            spec=suite.to_evaluation_spec(
                baseline=baseline.target, candidate=candidate.target
            ),
            dataset=fixture_dataset(),
            baseline=altered if side == "baseline" else baseline,
            candidate=altered if side == "candidate" else candidate,
        )


@mark.parametrize("drift", ["dataset", "slices"])
def test_comparison_cannot_relabel_evidence_with_inapplicable_suite(drift: str) -> None:
    suite = comparison_suite()
    baseline, candidate = suite_runs(suite)
    altered_suite = EvaluationSuiteVersion.create(
        name=suite.name,
        revision=2,
        dataset=(
            suite.dataset.model_copy(update={"revision": 2})
            if drift == "dataset"
            else suite.dataset
        ),
        evaluators=suite.evaluators,
        slices=("language/missing",) if drift == "slices" else suite.slices,
        execution=suite.execution,
        gates=suite.gates,
    )
    relabeled = tuple(
        RunResult.create(
            run_id=run.run_id,
            dataset=run.dataset,
            target=run.target,
            evaluators=run.evaluators,
            cases=run.cases,
            metrics=run.metrics,
            suite=altered_suite.artifact_ref,
        )
        for run in (baseline, candidate)
    )

    with raises(ComparisonConfigurationError, match="dataset"):
        compare_runs(
            suite=altered_suite,
            spec=altered_suite.to_evaluation_spec(
                baseline=baseline.target, candidate=candidate.target
            ),
            dataset=fixture_dataset(),
            baseline=relabeled[0],
            candidate=relabeled[1],
        )


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
