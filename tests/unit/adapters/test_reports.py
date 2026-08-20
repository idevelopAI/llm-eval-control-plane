import json
from xml.etree import ElementTree

from pytest import raises

from llm_eval_control_plane.adapters import ReportFormat, render_report
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
)


def ref(kind: ArtifactKind, name: str, revision: int, char: str) -> ArtifactRef:
    return ArtifactRef(
        kind=kind,
        name=name,
        revision=revision,
        digest="sha256:" + char * 64,
    )


DATASET = ref(ArtifactKind.DATASET, "dataset", 1, "d")
BASELINE = ref(ArtifactKind.TARGET, "target", 1, "a")
CANDIDATE = ref(ArtifactKind.TARGET, "target", 2, "b")
EVALUATOR = ref(ArtifactKind.EVALUATOR, "exact", 1, "e")


def decision(*, passed: bool = False) -> ReleaseDecision:
    candidate_mean = 1.0 if passed else 0.9
    aggregate = AggregateComparison(
        metric="quality.exact_match",
        slice="language/en",
        evaluator=EVALUATOR,
        baseline=MetricAggregate(
            attempted=10,
            scored=10,
            skipped=0,
            errors=0,
            mean=1.0,
        ),
        candidate=MetricAggregate(
            attempted=10,
            scored=10,
            skipped=0,
            errors=0,
            mean=candidate_mean,
        ),
        delta=candidate_mean - 1.0,
    )
    gate = GateResult(
        metric=aggregate.metric,
        slice=aggregate.slice,
        direction=MetricDirection.HIGHER_IS_BETTER,
        threshold=0.9,
        allowed_regression=0.05,
        aggregate=aggregate,
        coverage_passed=True,
        threshold_passed=True,
        regression_passed=passed,
        status=GateStatus.PASSED if passed else GateStatus.FAILED,
        failure_codes=() if passed else (GateFailureCode.REGRESSION,),
    )
    case = GateCaseComparison(
        metric=aggregate.metric,
        slice=aggregate.slice,
        case_id="case-en-001",
        slices=("language/en",),
        baseline=ComparisonValue(
            status=ComparisonValueStatus.SCORED,
            value=1.0,
        ),
        candidate=ComparisonValue(
            status=ComparisonValueStatus.SCORED,
            value=1.0 if passed else 0.0,
        ),
        delta=0.0 if passed else -1.0,
        baseline_passed=True,
        candidate_passed=passed,
        change=(CaseChange.UNCHANGED_PASSING if passed else CaseChange.NEWLY_FAILING),
    )
    return ReleaseDecision.create(
        spec_name="release-policy",
        dataset=DATASET,
        baseline=BASELINE,
        candidate=CANDIDATE,
        baseline_run_id="baseline-run",
        candidate_run_id="candidate-run",
        baseline_result_digest="sha256:" + "1" * 64,
        candidate_result_digest="sha256:" + "2" * 64,
        aggregates=(aggregate,),
        gates=(gate,),
        cases=(case,),
    )


def test_json_report_is_stable_machine_readable_evidence() -> None:
    report = render_report(decision(), ReportFormat.JSON)
    payload = json.loads(report)

    assert report.endswith("\n")
    assert payload["status"] == "failed"
    assert payload["gates"][0]["failure_codes"] == ["regression"]
    assert payload["cases"][0]["case_id"] == "case-en-001"


def test_markdown_report_identifies_gate_and_newly_failing_case() -> None:
    report = render_report(decision(), ReportFormat.MARKDOWN)

    assert "**Decision:** `FAILED`" in report
    assert "quality.exact_match" in report
    assert "language/en" in report
    assert "case-en-001" in report
    assert "regression" in report

    passed = render_report(decision(passed=True), ReportFormat.MARKDOWN)
    assert "**Decision:** `PASSED`" in passed
    assert "## Newly failing cases\n\nNone." in passed


def test_junit_report_maps_each_failed_gate_to_a_test_failure() -> None:
    report = render_report(decision(), ReportFormat.JUNIT)
    root = ElementTree.fromstring(report)

    assert root.tag == "testsuite"
    assert root.attrib == {
        "name": "release-policy",
        "tests": "1",
        "failures": "1",
        "errors": "0",
    }
    failure = root.find("./testcase/failure")
    assert failure is not None
    assert failure.attrib["type"] == "release_gate_failure"
    assert "allowed_regression=0.05" in (failure.text or "")

    passed = ElementTree.fromstring(
        render_report(decision(passed=True), ReportFormat.JUNIT)
    )
    assert passed.attrib["failures"] == "0"
    assert passed.find("./testcase/failure") is None


def test_unknown_report_format_fails_safely() -> None:
    with raises(ValueError, match="Unsupported release report format"):
        render_report(decision(), "yaml")  # type: ignore[arg-type]
