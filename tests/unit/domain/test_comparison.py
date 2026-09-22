from pydantic import TypeAdapter, ValidationError
from pytest import mark, raises

from llm_eval_control_plane.domain import (
    AggregateComparison,
    ArtifactKind,
    ArtifactRef,
    CaseChange,
    ComparisonValue,
    ComparisonValueStatus,
    ExecutionMode,
    GateCaseComparison,
    GateFailureCode,
    GateResult,
    GateStatus,
    MetricAggregate,
    MetricDirection,
    ReleaseDecision,
    ReleaseStatus,
)
from llm_eval_control_plane.domain.canonical import sha256_digest


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
            ArtifactKind.SUITE: "f",
        }[kind]
        * 64,
    )


DATASET = ref(ArtifactKind.DATASET, "dataset")
BASELINE = ref(ArtifactKind.TARGET, "target", 1)
CANDIDATE = ref(ArtifactKind.TARGET, "target", 2)
EVALUATOR = ref(ArtifactKind.EVALUATOR, "exact")
SUITE = ref(ArtifactKind.SUITE, "release-suite")


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
    *,
    passed: bool = True,
    baseline_run_id: str = "baseline",
    execution_mode: ExecutionMode = ExecutionMode.OFFLINE_DETERMINISTIC_FIXTURE,
    suite: ArtifactRef | None = None,
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
        execution_mode=execution_mode,
        suite=suite,
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

    payload = item.model_dump()
    payload["delta"] = None
    with raises(ValidationError, match="delta exists exactly"):
        GateCaseComparison.model_validate(payload)

    payload = item.model_dump()
    payload["baseline"] = {"status": "skipped", "value": None}
    payload["delta"] = None
    with raises(ValidationError, match="pass states exist exactly"):
        GateCaseComparison.model_validate(payload)

    payload = item.model_dump()
    payload["slices"] = ("language/en", "answerability/answerable")
    with raises(ValidationError, match="canonically ordered"):
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

    with raises(ValidationError, match="mean exists exactly"):
        MetricAggregate(
            attempted=10,
            scored=10,
            skipped=0,
            errors=0,
        )

    payload = aggregate().model_dump()
    payload["evaluator"] = DATASET.model_dump()
    with raises(ValidationError, match="must reference an evaluator"):
        AggregateComparison.model_validate(payload)

    payload = aggregate().model_dump()
    payload["evaluator"] = EVALUATOR.model_copy(update={"digest": None}).model_dump()
    with raises(ValidationError, match="resolved digest"):
        AggregateComparison.model_validate(payload)

    payload = aggregate().model_dump()
    payload["candidate"] = {
        "attempted": 11,
        "scored": 11,
        "skipped": 0,
        "errors": 0,
        "mean": 0.9,
    }
    with raises(ValidationError, match="attempt the same"):
        AggregateComparison.model_validate(payload)


def test_gate_result_requires_matching_failure_codes_and_status() -> None:
    failed = gate_result(passed=False)

    assert failed.status is GateStatus.FAILED
    assert failed.failure_codes == (GateFailureCode.REGRESSION,)

    payload = failed.model_dump()
    payload["failure_codes"] = ()
    with raises(ValidationError, match="failure codes"):
        GateResult.model_validate(payload)

    payload = failed.model_dump()
    payload["metric"] = "quality.other"
    with raises(ValidationError, match="reference its aggregate"):
        GateResult.model_validate(payload)

    payload = failed.model_dump()
    payload["status"] = GateStatus.PASSED
    with raises(ValidationError, match="status does not match"):
        GateResult.model_validate(payload)


def test_release_decision_is_content_addressed_and_run_id_independent() -> None:
    first = decision()
    second = decision(baseline_run_id="another-baseline-run")

    assert first.status is ReleaseStatus.PASSED
    assert first.decision_digest == second.decision_digest


def test_live_release_decision_covers_execution_mode() -> None:
    offline = decision()
    live = ReleaseDecision.create(
        spec_name=offline.spec_name,
        dataset=offline.dataset,
        baseline=offline.baseline,
        candidate=offline.candidate,
        baseline_run_id=offline.baseline_run_id,
        candidate_run_id=offline.candidate_run_id,
        baseline_result_digest=offline.baseline_result_digest,
        candidate_result_digest=offline.candidate_result_digest,
        aggregates=offline.aggregates,
        gates=offline.gates,
        cases=offline.cases,
        execution_mode=ExecutionMode.LIVE,
    )

    assert live.execution_mode is ExecutionMode.LIVE
    assert live.decision_digest != offline.decision_digest

    payload = live.model_dump()
    payload["execution_mode"] = ExecutionMode.OFFLINE_MOCK
    with raises(ValidationError, match="digest does not match"):
        ReleaseDecision.model_validate(payload)


def test_release_decision_fails_when_any_gate_fails_and_rejects_tampering() -> None:
    failed = decision(passed=False)

    assert failed.status is ReleaseStatus.FAILED

    payload = failed.model_dump()
    payload["decision_digest"] = "sha256:" + "0" * 64
    with raises(ValidationError, match="digest does not match"):
        ReleaseDecision.model_validate(payload)


def test_release_decision_rejects_invalid_artifacts_order_and_status() -> None:
    valid = decision()

    payload = valid.model_dump(mode="json")
    payload["dataset"] = BASELINE.model_dump(mode="json")
    with raises(ValidationError, match="resolved dataset"):
        ReleaseDecision.model_validate(payload)

    payload = valid.model_dump(mode="json")
    payload["baseline"] = DATASET.model_dump(mode="json")
    with raises(ValidationError, match="resolved target"):
        ReleaseDecision.model_validate(payload)

    payload = valid.model_dump(mode="json")
    second = payload["aggregates"][0].copy()
    second["metric"] = "performance.latency_ms"
    payload["aggregates"] = [payload["aggregates"][0], second]
    with raises(ValidationError, match="aggregates must be canonically ordered"):
        ReleaseDecision.model_validate(payload)

    payload = valid.model_dump(mode="json")
    second = payload["gates"][0].copy()
    second["metric"] = "performance.latency_ms"
    second["aggregate"] = second["aggregate"].copy()
    second["aggregate"]["metric"] = "performance.latency_ms"
    payload["gates"] = [payload["gates"][0], second]
    with raises(ValidationError, match="gates must be canonically ordered"):
        ReleaseDecision.model_validate(payload)

    payload = valid.model_dump(mode="json")
    second = payload["cases"][0].copy()
    second["metric"] = "performance.latency_ms"
    payload["cases"] = [payload["cases"][0], second]
    with raises(ValidationError, match="cases must be canonically ordered"):
        ReleaseDecision.model_validate(payload)

    payload = valid.model_dump(mode="json")
    payload["gates"] = [payload["gates"][0], payload["gates"][0]]
    with raises(ValidationError, match="gates must be unique"):
        ReleaseDecision.model_validate(payload)

    payload = valid.model_dump(mode="json")
    payload["status"] = ReleaseStatus.FAILED
    with raises(ValidationError, match="status does not match"):
        ReleaseDecision.model_validate(payload)


@mark.parametrize(
    ("execution_mode", "decision_digest", "document_digest"),
    (
        (
            ExecutionMode.OFFLINE_DETERMINISTIC_FIXTURE,
            "sha256:1f39e32cfb2a97b57a29c90bdf74f77387dd914d5a9af92378374d006641abd2",
            "sha256:37b9e0e238175557299d443df3eae6d3461025402302b6164f12b423905ef795",
        ),
        (
            ExecutionMode.OFFLINE_MOCK,
            "sha256:03f032bef6b82b11d6b80d0ce867a9c404ee47be3cf2f4ad9ef9bb0f114e844d",
            "sha256:b67aae70b0b51e2e77dc65cca1f692a37e7bf8c74ad0412bfbe0c505b802c6bc",
        ),
        (
            ExecutionMode.LIVE,
            "sha256:65b5c4f5c3aedfdefa4dcc81878db41f12eb85fb5d50fd094f497366bb73cec6",
            "sha256:38d00063d9a3bbde50c1ec774c1d0e19d157e8d866f7f101fe3d642248fdbbdb",
        ),
    ),
)
def test_unpinned_decisions_preserve_historical_digest_and_document_bytes(
    execution_mode: ExecutionMode, decision_digest: str, document_digest: str
) -> None:
    # Golden fingerprints were captured from the pre-suite v1/v2 implementation.
    release = decision(execution_mode=execution_mode)
    payload = release.model_dump(mode="json")
    assert release.decision_digest == decision_digest
    assert sha256_digest(payload) == document_digest
    assert "suite" not in release.model_dump()
    assert ReleaseDecision.model_validate_json(release.model_dump_json()).suite is None
    assert "suite" not in TypeAdapter(list[ReleaseDecision]).dump_python([release])[0]
    payload["suite"] = None
    assert ReleaseDecision.model_validate(payload).model_dump(
        mode="json"
    ) == release.model_dump(mode="json")


@mark.parametrize(
    "changed_suite",
    (
        SUITE.model_copy(update={"name": "another-suite"}),
        SUITE.model_copy(update={"revision": 2}),
        SUITE.model_copy(update={"digest": "sha256:" + "a" * 64}),
    ),
)
def test_decision_digest_pins_complete_suite_identity(
    changed_suite: ArtifactRef,
) -> None:
    pinned = decision(suite=SUITE)
    changed = decision(suite=changed_suite)
    assert pinned.decision_digest != changed.decision_digest
    assert pinned.decision_digest != decision().decision_digest
    assert pinned.model_dump(mode="json")["suite"] == SUITE.model_dump(mode="json")
    assert ReleaseDecision.model_validate_json(pinned.model_dump_json()) == pinned

    payload = pinned.model_dump()
    payload["suite"] = changed_suite.model_dump()
    with raises(ValidationError, match="digest does not match"):
        ReleaseDecision.model_validate(payload)
    payload.pop("suite")
    with raises(ValidationError, match="digest does not match"):
        ReleaseDecision.model_validate(payload)


@mark.parametrize("invalid_suite", (DATASET, SUITE.model_copy(update={"digest": None})))
def test_decision_suite_must_be_a_resolved_suite_artifact(
    invalid_suite: ArtifactRef,
) -> None:
    with raises(ValidationError, match="resolved suite artifact"):
        decision(suite=invalid_suite)


def test_pinned_decision_covers_execution_mode_including_fixture_mode() -> None:
    decisions = [decision(suite=SUITE, execution_mode=mode) for mode in ExecutionMode]
    # Pin the v3 projection, including the explicit execution mode for fixtures.
    assert [release.decision_digest for release in decisions] == [
        "sha256:755ae05ac86bc9e78cbb2d9221a36c53a6d6a2ac52c2b82192977eaf2f66bef2",
        "sha256:96d43a6c1276e4f134a347d3ca5397590bd65eb77f0ef688cd61cb9aacc1f12f",
        "sha256:f7524ddd302d1abf45d5876430b49a6b6c9923393aa042b22044ff20e1296cd1",
    ]
    for release in decisions:
        payload = release.model_dump()
        payload["execution_mode"] = (
            ExecutionMode.LIVE
            if release.execution_mode is ExecutionMode.OFFLINE_DETERMINISTIC_FIXTURE
            else ExecutionMode.OFFLINE_DETERMINISTIC_FIXTURE
        )
        with raises(ValidationError, match="digest does not match"):
            ReleaseDecision.model_validate(payload)


def test_decision_serializer_preserves_descriptive_schema_and_field_selection() -> None:
    properties = ReleaseDecision.model_json_schema(mode="serialization")["properties"]
    assert {"suite", "spec_name", "decision_digest"} <= properties.keys()
    assert decision().model_dump(include={"spec_name", "suite"}) == {
        "spec_name": "release-policy"
    }
    assert "suite" not in decision(suite=SUITE).model_dump(exclude={"suite"})
