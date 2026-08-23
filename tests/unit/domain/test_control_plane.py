from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from pytest import raises

from llm_eval_control_plane.domain import CanonicalJson, DatasetVersion, EvaluationCase
from llm_eval_control_plane.domain.artifacts import ArtifactKind, ArtifactRef
from llm_eval_control_plane.domain.canonical import sha256_digest
from llm_eval_control_plane.domain.control_plane import (
    ComparisonJobPayload,
    CursorPage,
    DatasetRecord,
    ExecutionContract,
    JobAttemptRecord,
    JobAttemptStatus,
    JobKind,
    JobRecord,
    JobStatus,
    JobTransitionError,
    RunJobPayload,
    ScenarioOverride,
)
from llm_eval_control_plane.domain.evaluation import (
    EvaluationSpec,
    MetricDirection,
    MetricGate,
)
from llm_eval_control_plane.domain.results import ExecutionMode

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


def job(*, status: JobStatus = JobStatus.QUEUED) -> JobRecord:
    started = status not in {JobStatus.QUEUED, JobStatus.CANCELED}
    return JobRecord(
        job_id="job-001",
        kind=JobKind.RUN,
        status=status,
        idempotency_key="request-001",
        request_digest=sha256_digest({"request": 1}),
        resource_id="run-001",
        attempt_count=1 if started else 0,
        max_attempts=3,
        available_at=NOW,
        error_code="execution_failed" if status is JobStatus.FAILED else None,
        created_at=NOW,
        updated_at=NOW,
    )


def test_job_lifecycle_requires_queued_running_terminal_order() -> None:
    queued = job()
    running = queued.transition_to(JobStatus.RUNNING, at=NOW + timedelta(seconds=1))
    succeeded = running.transition_to(
        JobStatus.SUCCEEDED, at=NOW + timedelta(seconds=2)
    )

    assert running.status is JobStatus.RUNNING
    assert running.attempt_count == 1
    assert succeeded.status is JobStatus.SUCCEEDED
    assert queued.status is JobStatus.QUEUED

    with raises(JobTransitionError, match="not allowed"):
        queued.transition_to(
            JobStatus.FAILED,
            at=NOW + timedelta(seconds=1),
            error_code="execution_failed",
        )
    with raises(JobTransitionError, match="not allowed"):
        succeeded.transition_to(JobStatus.RUNNING, at=NOW + timedelta(seconds=3))


def test_retry_and_cancellation_transitions_are_bounded() -> None:
    running = job().transition_to(JobStatus.RUNNING, at=NOW)
    available_at = NOW + timedelta(seconds=10)
    queued = running.transition_to(
        JobStatus.QUEUED,
        at=NOW + timedelta(seconds=1),
        available_at=available_at,
    )

    assert queued.attempt_count == 1
    assert queued.available_at == available_at
    with raises(JobTransitionError, match="not available"):
        queued.transition_to(JobStatus.RUNNING, at=NOW + timedelta(seconds=5))

    second = queued.transition_to(JobStatus.RUNNING, at=available_at)
    requested = second.request_cancellation(at=available_at + timedelta(seconds=1))
    canceled = requested.transition_to(
        JobStatus.CANCELED,
        at=available_at + timedelta(seconds=2),
    )
    assert requested.status is JobStatus.CANCEL_REQUESTED
    assert canceled.status is JobStatus.CANCELED

    queued_cancel = job().request_cancellation(at=NOW + timedelta(seconds=1))
    assert queued_cancel.status is JobStatus.CANCELED
    assert queued_cancel.attempt_count == 0


def test_last_attempt_cannot_be_requeued() -> None:
    running = job(status=JobStatus.RUNNING).model_copy(
        update={"attempt_count": 2, "max_attempts": 2}
    )
    running = JobRecord.model_validate(running.model_dump(mode="python"))

    with raises(JobTransitionError, match="no retry"):
        running.transition_to(
            JobStatus.QUEUED,
            at=NOW + timedelta(seconds=1),
            available_at=NOW + timedelta(seconds=2),
        )


def test_failed_transition_requires_safe_code_and_monotonic_time() -> None:
    running = job().transition_to(JobStatus.RUNNING, at=NOW + timedelta(seconds=2))

    with raises(JobTransitionError, match="safe error code"):
        running.transition_to(JobStatus.FAILED, at=NOW + timedelta(seconds=3))
    with raises(JobTransitionError, match="backwards"):
        running.transition_to(
            JobStatus.FAILED,
            at=NOW + timedelta(seconds=1),
            error_code="execution_failed",
        )

    failed = running.transition_to(
        JobStatus.FAILED,
        at=NOW + timedelta(seconds=3),
        error_code="execution_failed",
    )
    assert failed.error_code == "execution_failed"
    assert (
        failed.transition_to(
            JobStatus.FAILED,
            at=NOW + timedelta(seconds=4),
            error_code="execution_failed",
        )
        is failed
    )
    with raises(JobTransitionError, match="stored state"):
        failed.transition_to(
            JobStatus.FAILED,
            at=NOW + timedelta(seconds=4),
            error_code="different_error",
        )


def test_records_require_aware_utc_time_and_remain_frozen() -> None:
    dataset = DatasetVersion.create(
        name="fixture",
        revision=1,
        cases=(
            EvaluationCase(
                case_id="case-001",
                input=CanonicalJson.from_value({"value": "answer"}),
            ),
        ),
    )
    offset = datetime.fromisoformat("2026-08-20T14:00:00+02:00")
    record = DatasetRecord(dataset=dataset, created_at=offset)

    assert record.created_at == NOW
    with raises(ValidationError, match="timezone"):
        DatasetRecord(dataset=dataset, created_at=datetime(2026, 8, 20, 12))
    with raises(ValidationError, match="frozen"):
        record.created_at = NOW + timedelta(seconds=1)


def test_job_record_rejects_inconsistent_failure_evidence_and_times() -> None:
    with raises(ValidationError, match="only failed"):
        job().model_copy(update={"error_code": "execution_failed"}).model_validate(
            {
                **job().model_dump(mode="python"),
                "error_code": "execution_failed",
            }
        )

    with raises(ValidationError, match="precede"):
        JobRecord(
            **{
                **job().model_dump(mode="python"),
                "updated_at": NOW - timedelta(seconds=1),
            }
        )

    with raises(ValidationError, match="attempt remaining"):
        JobRecord.model_validate(
            {
                **job().model_dump(mode="python"),
                "attempt_count": 3,
                "max_attempts": 3,
            }
        )


def _artifact(kind: ArtifactKind, name: str, revision: int) -> ArtifactRef:
    return ArtifactRef(
        kind=kind,
        name=name,
        revision=revision,
        digest=sha256_digest({"kind": kind.value, "name": name, "revision": revision}),
    )


def test_resolved_worker_payloads_are_typed_canonical_and_content_addressed() -> None:
    dataset = _artifact(ArtifactKind.DATASET, "fixture", 1)
    target = _artifact(ArtifactKind.TARGET, "fake/candidate", 2)
    evaluator = _artifact(ArtifactKind.EVALUATOR, "builtin/exact_match", 1)
    contract = ExecutionContract(
        adapter="deterministic_fake",
        evaluator_names=("exact_match",),
        target=target,
        evaluators=(evaluator,),
        execution_mode=ExecutionMode.OFFLINE_MOCK,
    )
    run_payload = RunJobPayload(
        dataset=dataset,
        target_name=target.name,
        target_revision=target.revision,
        adapter=contract.adapter,
        evaluator_names=contract.evaluator_names,
        scenario_overrides=(
            ScenarioOverride(case_id="case/001", scenario="uppercase"),
        ),
        execution_contract=contract,
    )

    assert run_payload.kind is JobKind.RUN
    assert run_payload.payload_digest == sha256_digest(
        run_payload.model_dump(mode="json")
    )
    with raises(ValidationError, match="must be ordered"):
        RunJobPayload(
            **{
                **run_payload.model_dump(mode="python"),
                "scenario_overrides": (
                    ScenarioOverride(case_id="case-002", scenario="echo"),
                    ScenarioOverride(case_id="case-001", scenario="echo"),
                ),
            }
        )

    baseline = _artifact(ArtifactKind.TARGET, "fake/baseline", 1)
    spec = EvaluationSpec(
        name="release-policy",
        dataset=dataset,
        baseline=baseline,
        candidate=target,
        gates=(
            MetricGate(
                metric="quality.exact_match",
                direction=MetricDirection.HIGHER_IS_BETTER,
                threshold=1.0,
            ),
        ),
    )
    comparison_payload = ComparisonJobPayload(
        dataset=dataset,
        baseline_run_id="run-baseline",
        baseline_result_digest=sha256_digest({"run": "baseline"}),
        candidate_run_id="run-candidate",
        candidate_result_digest=sha256_digest({"run": "candidate"}),
        spec=spec,
    )
    assert comparison_payload.kind is JobKind.COMPARISON
    assert comparison_payload.payload_digest == sha256_digest(
        comparison_payload.model_dump(mode="json")
    )


def test_attempt_records_exclude_tokens_and_validate_terminal_evidence() -> None:
    running = JobAttemptRecord(
        job_id="job-001",
        attempt_number=1,
        status=JobAttemptStatus.RUNNING,
        started_at=NOW,
        heartbeat_at=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    succeeded = JobAttemptRecord(
        **{
            **running.model_dump(mode="python"),
            "status": JobAttemptStatus.SUCCEEDED,
            "finished_at": NOW + timedelta(seconds=1),
        }
    )

    assert succeeded.error_code is None
    assert "lease_token" not in succeeded.model_dump()
    with raises(ValidationError, match="safe error code"):
        JobAttemptRecord(
            **{
                **running.model_dump(mode="python"),
                "status": JobAttemptStatus.RETRY_SCHEDULED,
                "finished_at": NOW + timedelta(seconds=1),
            }
        )


def test_cursor_page_is_strict_and_immutable() -> None:
    page = CursorPage[int](items=(1, 2), next_cursor="opaque")

    assert page.items == (1, 2)
    with raises(ValidationError, match="extra"):
        CursorPage[int](items=(1,), unexpected=True)  # type: ignore[call-arg]
