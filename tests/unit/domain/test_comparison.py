from pydantic import ValidationError
from pytest import raises

from llm_eval_control_plane.domain import (
    AggregateComparison,
    ArtifactKind,
    ArtifactRef,
    CaseChange,
    ComparisonValue,
    ComparisonValueStatus,
    GateCaseComparison,
    GateFailureCode,
    GateResult,
    GateStatus,
    MetricAggregate,
    MetricDirection,
    ReleaseDecision,
    ReleaseStatus,
)


def ref(kind: ArtifactKind, name: str, revision: int = 1) -> ArtifactRef:
    return ArtifactRef(
        kind=kind,
        name=name,
        revision=revision,
        digest="sha256:"
        + {
            ArtifactKind.DATASET: "d",
            ArtifactKind.TARGET: "a",
            ArtifactKind.EVALUATOR: "e",
        }[kind]
        * 64,
    )


DATASET = ref(ArtifactKind.DATASET, "dataset")
BASELINE = ref(ArtifactKind.TARGET, "target", 1)
CANDIDATE = ref(ArtifactKind.TARGET, "target", 2)
EVALUATOR = ref(ArtifactKind.EVALUATOR, "exact")


def aggregate(*, baseline: float = 1.0, candidate: float = 0.9) -> AggregateComparison:
    return AggregateComparison(
        metric="quality.exact_match",
        evaluator=EVALUATOR,
        baseline=MetricAggregate(
            attempted=10,
            scored=10,
            skipped=0,
            errors=0,
            mean=baseline,
        ),
        candidate=MetricAggregate(
            attempted=10,
            scored=10,
            skipped=0,
            errors=0,
            mean=candidate,
        ),
        delta=candidate - baseline,
    )


def gate_result(*, passed: bool = True) -> GateResult:
    item = aggregate()
    return GateResult(
        metric=item.metric,
        direction=MetricDirection.HIGHER_IS_BETTER,
        threshold=0.9,
        allowed_regression=0.1,
        aggregate=item,
        coverage_passed=True,
        threshold_passed=True,
        regression_passed=passed,
        status=GateStatus.PASSED if passed else GateStatus.FAILED,
        failure_codes=() if passed else (GateFailureCode.REGRESSION,),
    )


def case_comparison() -> GateCaseComparison:
    return GateCaseComparison(
        metric="quality.exact_match",
        case_id="case-001",
        slices=("language/en",),
        baseline=ComparisonValue(
            status=ComparisonValueStatus.SCORED,
            value=1.0,
        ),
        candidate=ComparisonValue(
            status=ComparisonValueStatus.SCORED,
            value=0.0,
        ),
        delta=-1.0,
        baseline_passed=True,
        candidate_passed=False,
        change=CaseChange.NEWLY_FAILING,
    )


def decision(
    *, passed: bool = True, baseline_run_id: str = "baseline"
) -> ReleaseDecision:
    item = aggregate()
    gate = gate_result(passed=passed)
    return ReleaseDecision.create(
        spec_name="release-policy",
        dataset=DATASET,
        baseline=BASELINE,
        candidate=CANDIDATE,
        baseline_run_id=baseline_run_id,
        candidate_run_id="candidate",
        baseline_result_digest="sha256:" + "b" * 64,
        candidate_result_digest="sha256:" + "c" * 64,
        aggregates=(item,),
        gates=(gate,),
        cases=(case_comparison(),),
    )


def test_comparison_value_requires_number_only_when_scored() -> None:
    assert ComparisonValue(status=ComparisonValueStatus.SKIPPED).value is None

    with raises(ValidationError, match="only scored"):
        ComparisonValue(status=ComparisonValueStatus.SCORED)
    with raises(ValidationError, match="only scored"):
        ComparisonValue(status=ComparisonValueStatus.ERROR, value=1.0)


def test_case_comparison_derives_newly_failing_state() -> None:
    item = case_comparison()

    assert item.delta == -1.0
    assert item.change is CaseChange.NEWLY_FAILING

    payload = item.model_dump()
    payload["change"] = CaseChange.UNCHANGED_PASSING
    with raises(ValidationError, match="change does not match"):
        GateCaseComparison.model_validate(payload)


def test_aggregate_requires_complete_coverage_accounting() -> None:
    with raises(ValidationError, match="outcome counts"):
        MetricAggregate(
            attempted=10,
            scored=9,
            skipped=0,
            errors=0,
            mean=1.0,
        )

    payload = aggregate().model_dump()
    payload["delta"] = None
    with raises(ValidationError, match="delta exists exactly"):
        AggregateComparison.model_validate(payload)


def test_gate_result_requires_matching_failure_codes_and_status() -> None:
    failed = gate_result(passed=False)

    assert failed.status is GateStatus.FAILED
    assert failed.failure_codes == (GateFailureCode.REGRESSION,)

    payload = failed.model_dump()
    payload["failure_codes"] = ()
    with raises(ValidationError, match="failure codes"):
        GateResult.model_validate(payload)


def test_release_decision_is_content_addressed_and_run_id_independent() -> None:
    first = decision()
    second = decision(baseline_run_id="another-baseline-run")

    assert first.status is ReleaseStatus.PASSED
    assert first.decision_digest == second.decision_digest


def test_release_decision_fails_when_any_gate_fails_and_rejects_tampering() -> None:
    failed = decision(passed=False)

    assert failed.status is ReleaseStatus.FAILED

    payload = failed.model_dump()
    payload["decision_digest"] = "sha256:" + "0" * 64
    with raises(ValidationError, match="digest does not match"):
        ReleaseDecision.model_validate(payload)
