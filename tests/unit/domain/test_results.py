from pydantic import ValidationError
from pytest import raises

from llm_eval_control_plane.domain import (
    ArtifactKind,
    ArtifactRef,
    CanonicalJson,
    CaseResult,
    CaseResultStatus,
    ExecutionFailure,
    FailureCode,
    FailureStage,
    MetricSummary,
    RunResult,
    RunStatus,
    ScoredObservation,
    TargetObservation,
    TargetResponse,
    TokenUsage,
)


def ref(kind: ArtifactKind, name: str) -> ArtifactRef:
    digest_character = {
        ArtifactKind.DATASET: "d",
        ArtifactKind.TARGET: "a",
        ArtifactKind.EVALUATOR: "e",
    }[kind]
    return ArtifactRef(
        kind=kind,
        name=name,
        revision=1,
        digest="sha256:" + digest_character * 64,
    )


DATASET = ref(ArtifactKind.DATASET, "dataset")
TARGET = ref(ArtifactKind.TARGET, "target")
EVALUATOR = ref(ArtifactKind.EVALUATOR, "evaluator")


def target_observation() -> TargetObservation:
    return TargetObservation(
        response=TargetResponse(
            output=CanonicalJson.from_value("answer"),
            usage=TokenUsage(input_units=2, output_units=1),
        ),
        latency_ms=5.0,
    )


def completed_case(case_id: str = "case-1") -> CaseResult:
    return CaseResult(
        case_id=case_id,
        status=CaseResultStatus.COMPLETED,
        target=target_observation(),
        observations=(
            ScoredObservation(
                metric="exact_match",
                evaluator=EVALUATOR,
                value=1.0,
                reason_code="matched",
            ),
        ),
    )


def summary(*, errors: int = 0, scored: int = 1) -> MetricSummary:
    return MetricSummary(
        metric="exact_match",
        evaluator=EVALUATOR,
        attempted=1,
        scored=scored,
        skipped=0,
        errors=errors,
        mean=1.0 if scored else None,
    )


def test_case_result_validates_completed_and_target_failed_states() -> None:
    assert completed_case().status is CaseResultStatus.COMPLETED

    failed = CaseResult(
        case_id="case-2",
        status=CaseResultStatus.TARGET_FAILED,
        target_failure=ExecutionFailure(
            stage=FailureStage.TARGET,
            code=FailureCode.INVALID_TARGET_OUTPUT,
            message="Target result failed contract validation",
        ),
    )
    assert failed.target is None

    with raises(ValidationError, match="require only a target failure"):
        CaseResult(case_id="invalid", status=CaseResultStatus.TARGET_FAILED)


def test_case_result_rejects_duplicate_observations() -> None:
    observation = completed_case().observations[0]
    with raises(ValidationError, match="unique evaluator metrics"):
        CaseResult(
            case_id="duplicate",
            status=CaseResultStatus.COMPLETED,
            target=target_observation(),
            observations=(observation, observation),
        )


def test_metric_summary_counts_every_attempt_and_requires_mean() -> None:
    assert summary().mean == 1.0

    with raises(ValidationError, match="must equal attempted"):
        MetricSummary(
            metric="exact_match",
            evaluator=EVALUATOR,
            attempted=2,
            scored=1,
            skipped=0,
            errors=0,
            mean=1.0,
        )
    with raises(ValidationError, match="exists exactly"):
        MetricSummary(
            metric="exact_match",
            evaluator=EVALUATOR,
            attempted=1,
            scored=0,
            skipped=1,
            errors=0,
            mean=0.0,
        )


def test_run_result_is_sorted_content_addressed_and_run_id_independent() -> None:
    cases = (completed_case("case-b"), completed_case("case-a"))
    first = RunResult.create(
        run_id="run-one",
        dataset=DATASET,
        target=TARGET,
        evaluators=(EVALUATOR,),
        cases=cases,
        metrics=(
            MetricSummary(
                metric="exact_match",
                evaluator=EVALUATOR,
                attempted=2,
                scored=2,
                skipped=0,
                errors=0,
                mean=1.0,
            ),
        ),
    )
    second = first.model_copy(update={"run_id": "run-two"})

    assert first.status is RunStatus.COMPLETED
    assert [case.case_id for case in first.cases] == ["case-a", "case-b"]
    assert first.result_digest == second.result_digest


def test_run_result_rejects_tampering_and_inconsistent_status() -> None:
    run = RunResult.create(
        run_id="run",
        dataset=DATASET,
        target=TARGET,
        evaluators=(EVALUATOR,),
        cases=(completed_case(),),
        metrics=(summary(),),
    )
    payload = run.model_dump()
    payload["result_digest"] = "sha256:" + "0" * 64
    with raises(ValidationError, match="digest does not match"):
        RunResult.model_validate(payload)

    payload = run.model_dump()
    payload["status"] = RunStatus.COMPLETED_WITH_FAILURES
    with raises(ValidationError, match="status does not match"):
        RunResult.model_validate(payload)
