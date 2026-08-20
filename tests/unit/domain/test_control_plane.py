from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from pytest import raises

from llm_eval_control_plane.domain import CanonicalJson, DatasetVersion, EvaluationCase
from llm_eval_control_plane.domain.canonical import sha256_digest
from llm_eval_control_plane.domain.control_plane import (
    CursorPage,
    DatasetRecord,
    JobKind,
    JobRecord,
    JobStatus,
    JobTransitionError,
)

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


def job(*, status: JobStatus = JobStatus.QUEUED) -> JobRecord:
    return JobRecord(
        job_id="job-001",
        kind=JobKind.RUN,
        status=status,
        idempotency_key="request-001",
        request_digest=sha256_digest({"request": 1}),
        resource_id="run-001",
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


def test_cursor_page_is_strict_and_immutable() -> None:
    page = CursorPage[int](items=(1, 2), next_cursor="opaque")

    assert page.items == (1, 2)
    with raises(ValidationError, match="extra"):
        CursorPage[int](items=(1,), unexpected=True)  # type: ignore[call-arg]
