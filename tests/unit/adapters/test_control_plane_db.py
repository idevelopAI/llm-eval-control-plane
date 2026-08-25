import asyncio
import base64
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import cast

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from pytest import MonkeyPatch, fixture, mark, raises
from sqlalchemy import (
    create_engine,
    delete,
    event,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from llm_eval_control_plane.adapters import control_plane_db
from llm_eval_control_plane.adapters.control_plane_db import (
    CONTROL_PLANE_METADATA,
    ConcurrentTransitionError,
    ControlPlaneRepositoryError,
    CorruptRecordError,
    IdempotencyConflictError,
    IllegalJobTransitionError,
    ImmutableRecordConflictError,
    InvalidCursorError,
    LeaseLostError,
    PayloadTooLargeError,
    RecordNotFoundError,
    ResourceAlreadySubmittedError,
    SqlAlchemyControlPlaneRepository,
    _aware,
    _encode_cursor,
    datasets_table,
    job_attempts_table,
    job_payloads_table,
    jobs_table,
    release_decisions_table,
    runs_table,
)
from llm_eval_control_plane.adapters.fake_target import DeterministicFakeTarget
from llm_eval_control_plane.adapters.scorers import (
    BuiltInEvaluatorKind,
    build_evaluators,
)
from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.application.comparison import compare_runs
from llm_eval_control_plane.application.runner import InProcessRunner
from llm_eval_control_plane.domain import (
    CanonicalJson,
    DatasetVersion,
    EvaluationCase,
    EvaluationSpec,
    MetricDirection,
    MetricGate,
)
from llm_eval_control_plane.domain.canonical import canonical_json_bytes, sha256_digest
from llm_eval_control_plane.domain.comparison import ReleaseDecision, ReleaseStatus
from llm_eval_control_plane.domain.control_plane import (
    ComparisonJobPayload,
    DatasetRecord,
    JobAttemptStatus,
    JobKind,
    JobPayload,
    JobRecord,
    JobStatus,
    ReleaseDecisionRecord,
    RunJobPayload,
    RunRecord,
)
from llm_eval_control_plane.domain.results import RunResult

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
LEASE_TOKEN_A = "lease_token_a_0123456789abcdef0123456789abcdef"
LEASE_TOKEN_B = "lease_token_b_0123456789abcdef0123456789abcdef"
TRACEPARENT_A = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TRACEPARENT_B = "00-7a3ce929d0e0e47364bf92f3577b34da-0ba902b700f067aa-00"


class SequenceClock:
    def __init__(self, values: tuple[float, ...]) -> None:
        self._values: Iterator[float] = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def dataset(
    *, name: str = "fixture", revision: int = 1, expected: str = "answer"
) -> DatasetVersion:
    return DatasetVersion.create(
        name=name,
        revision=revision,
        cases=(
            EvaluationCase(
                case_id="case-001",
                input=CanonicalJson.from_value({"scenario": "echo", "value": expected}),
                expected=CanonicalJson.from_value(expected),
            ),
        ),
    )


def execute(
    dataset_version: DatasetVersion,
    *,
    run_id: str,
    target_revision: int = 1,
) -> RunResult:
    return asyncio.run(
        InProcessRunner(clock=SequenceClock((0.0, 0.005))).run(
            run_id=run_id,
            dataset=dataset_version,
            target=DeterministicFakeTarget(revision=target_revision),
            evaluators=build_evaluators((BuiltInEvaluatorKind.EXACT_MATCH,)),
        )
    )


def run_usage(result: RunResult) -> tuple[int, int]:
    return (
        sum(
            case.target.response.usage.input_units
            for case in result.cases
            if case.target is not None
        ),
        sum(
            case.target.response.usage.output_units
            for case in result.cases
            if case.target is not None
        ),
    )


def decision(
    dataset_version: DatasetVersion,
    baseline: RunResult,
    candidate: RunResult,
) -> ReleaseDecision:
    spec = EvaluationSpec(
        name="release-policy",
        dataset=dataset_version.artifact_ref,
        baseline=baseline.target.model_copy(update={"digest": None}),
        candidate=candidate.target.model_copy(update={"digest": None}),
        gates=(
            MetricGate(
                metric="quality.exact_match",
                direction=MetricDirection.HIGHER_IS_BETTER,
                threshold=1.0,
            ),
        ),
    )
    return compare_runs(
        spec=spec,
        dataset=dataset_version,
        baseline=baseline,
        candidate=candidate,
    )


def run_payload(
    dataset_version: DatasetVersion | None = None,
    *,
    target_name: str = "fake/worker",
    target_revision: int = 1,
) -> RunJobPayload:
    selected = dataset() if dataset_version is None else dataset_version
    executor = DeterministicEvaluationExecutor()
    evaluator_names = ("exact_match",)
    return RunJobPayload(
        dataset=selected.artifact_ref,
        target_name=target_name,
        target_revision=target_revision,
        adapter="deterministic_fake",
        evaluator_names=evaluator_names,
        execution_contract=executor.validate(
            target_name=target_name,
            target_revision=target_revision,
            adapter="deterministic_fake",
            evaluator_names=evaluator_names,
            scenario_overrides={},
        ),
    )


def comparison_payload(
    dataset_version: DatasetVersion,
    baseline: RunResult,
    candidate: RunResult,
) -> ComparisonJobPayload:
    policy = EvaluationSpec(
        name="release-policy",
        dataset=dataset_version.artifact_ref,
        baseline=baseline.target.model_copy(update={"digest": None}),
        candidate=candidate.target.model_copy(update={"digest": None}),
        gates=(
            MetricGate(
                metric="quality.exact_match",
                direction=MetricDirection.HIGHER_IS_BETTER,
                threshold=1.0,
            ),
        ),
    )
    return ComparisonJobPayload(
        dataset=dataset_version.artifact_ref,
        baseline_run_id=baseline.run_id,
        baseline_result_digest=baseline.result_digest,
        candidate_run_id=candidate.run_id,
        candidate_result_digest=candidate.result_digest,
        spec=policy,
    )


def job(
    *,
    job_id: str = "job-001",
    kind: JobKind = JobKind.RUN,
    idempotency_key: str = "request-001",
    resource_id: str = "run-001",
    request: object = "same",
    created_at: datetime = NOW,
    max_attempts: int = 3,
    traceparent: str | None = None,
) -> JobRecord:
    return JobRecord(
        job_id=job_id,
        kind=kind,
        status=JobStatus.QUEUED,
        idempotency_key=idempotency_key,
        request_digest=sha256_digest(request),
        resource_id=resource_id,
        attempt_count=0,
        max_attempts=max_attempts,
        available_at=created_at,
        created_at=created_at,
        updated_at=created_at,
        traceparent=traceparent,
    )


def activate_job(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
    record: JobRecord,
    payload: JobPayload,
    *,
    lease_token: str = LEASE_TOKEN_A,
    worker_id: str = "worker-a",
    expired: bool = False,
) -> JobRecord:
    """Seed one SQLite attempt so portable fenced mutations can be exercised."""
    repository.begin_job(record, payload)
    with engine.begin() as connection:
        database_now = repository._database_now(connection)
        transition_at = max(database_now, record.available_at, record.updated_at)
        running = record.transition_to(JobStatus.RUNNING, at=transition_at)
        changed = connection.execute(
            update(jobs_table)
            .where(jobs_table.c.job_id == record.job_id, jobs_table.c.version == 0)
            .values(
                status=running.status.value,
                attempt_count=running.attempt_count,
                updated_at=running.updated_at,
                version=1,
            )
        )
        assert changed.rowcount == 1
        if expired:
            started_at = database_now - timedelta(seconds=3)
            heartbeat_at = database_now - timedelta(seconds=2)
            lease_expires_at = database_now - timedelta(seconds=1)
        else:
            started_at = database_now
            heartbeat_at = database_now
            lease_expires_at = database_now + timedelta(hours=1)
        connection.execute(
            insert(job_attempts_table).values(
                job_id=record.job_id,
                attempt_number=1,
                status=JobAttemptStatus.RUNNING.value,
                worker_id=worker_id,
                lease_token=lease_token,
                error_code=None,
                started_at=started_at,
                heartbeat_at=heartbeat_at,
                lease_expires_at=lease_expires_at,
                finished_at=None,
            )
        )
    return repository.get_job(record.job_id)


@fixture
def engine(tmp_path: Path, monkeypatch: MonkeyPatch) -> Iterator[Engine]:
    database = tmp_path / "control-plane.sqlite3"
    url = f"sqlite+pysqlite:///{database}"
    monkeypatch.setenv("CONTROL_PLANE_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    value = create_engine(url)

    @event.listens_for(value, "connect")
    def enable_foreign_keys(connection: object, _record: object) -> None:
        cursor = connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    yield value
    value.dispose()


@fixture
def repository(engine: Engine) -> SqlAlchemyControlPlaneRepository:
    return SqlAlchemyControlPlaneRepository(engine)


def test_migration_matches_metadata_and_readiness(engine: Engine) -> None:
    repository = SqlAlchemyControlPlaneRepository(engine)
    expected_tables = set(CONTROL_PLANE_METADATA.tables)

    assert repository.schema_is_current() is True
    repository.check_health()
    assert expected_tables <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, CONTROL_PLANE_METADATA) == []


def test_operational_snapshot_uses_persisted_fixed_cardinality_aggregates(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    assert repository.operational_snapshot() == control_plane_db.OperationalSnapshot(
        queued_jobs=0,
        failed_jobs=0,
        input_units=0,
        output_units=0,
    )

    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    direct_result = execute(data, run_id="run-operational-direct")
    completed_result = execute(data, run_id="run-operational-completed")
    repository.put_run(RunRecord(result=direct_result, created_at=NOW))

    queued = job(
        job_id="job-operational-queued",
        idempotency_key="request-operational-queued",
        resource_id="run-operational-queued",
    )
    repository.begin_job(queued, run_payload(data))

    failed = job(
        job_id="job-operational-failed",
        idempotency_key="request-operational-failed",
        resource_id="run-operational-failed",
    )
    activate_job(engine, repository, failed, run_payload(data))
    repository.fail_job(
        failed.job_id,
        1,
        LEASE_TOKEN_A,
        error_code="execution_failed",
    )

    completed = job(
        job_id="job-operational-completed",
        idempotency_key="request-operational-completed",
        resource_id=completed_result.run_id,
    )
    activate_job(engine, repository, completed, run_payload(data))
    repository.complete_run(
        completed.job_id,
        RunRecord(result=completed_result, created_at=NOW),
        attempt_number=1,
        lease_token=LEASE_TOKEN_A,
    )

    direct_input, direct_output = run_usage(direct_result)
    completed_input, completed_output = run_usage(completed_result)
    assert repository.operational_snapshot() == control_plane_db.OperationalSnapshot(
        queued_jobs=1,
        failed_jobs=1,
        input_units=direct_input + completed_input,
        output_units=direct_output + completed_output,
    )
    with engine.connect() as connection:
        rows = {
            row["run_id"]: (row["input_units"], row["output_units"])
            for row in connection.execute(
                select(
                    runs_table.c.run_id,
                    runs_table.c.input_units,
                    runs_table.c.output_units,
                )
            ).mappings()
        }
    assert rows == {
        direct_result.run_id: (direct_input, direct_output),
        completed_result.run_id: (completed_input, completed_output),
    }


def test_run_usage_indexes_are_nonnegative_and_match_canonical_evidence(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    result = execute(data, run_id="run-usage-integrity")
    repository.put_run(RunRecord(result=result, created_at=NOW))

    with raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            update(runs_table)
            .where(runs_table.c.run_id == result.run_id)
            .values(input_units=-1)
        )

    with engine.begin() as connection:
        connection.execute(
            update(runs_table)
            .where(runs_table.c.run_id == result.run_id)
            .values(output_units=runs_table.c.output_units + 1)
        )
    with raises(CorruptRecordError, match="indexes") as captured:
        repository.get_run(result.run_id)
    assert result.run_id not in str(captured.value)


def test_dataset_is_append_only_idempotent_and_digest_is_not_unique(
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    first = DatasetRecord(dataset=dataset(), created_at=NOW)
    same_digest_other_identity = DatasetRecord(
        dataset=dataset(name="fixture-copy", revision=2),
        created_at=NOW + timedelta(seconds=1),
    )

    assert repository.put_dataset(first) == first
    assert repository.put_dataset(first) == first
    repository.put_dataset(same_digest_other_identity)
    assert same_digest_other_identity.dataset.digest == first.dataset.digest

    different = DatasetRecord(dataset=dataset(expected="different"), created_at=NOW)
    with raises(ImmutableRecordConflictError, match="different evidence"):
        repository.put_dataset(different)
    assert repository.get_dataset("fixture", 1) == first


def test_document_size_is_bounded_before_insert(engine: Engine) -> None:
    repository = SqlAlchemyControlPlaneRepository(engine, max_document_bytes=16)

    with raises(PayloadTooLargeError, match="size limit"):
        repository.put_dataset(DatasetRecord(dataset=dataset(), created_at=NOW))

    assert not engine.connect().execute(select(datasets_table)).first()


def test_begin_job_identifies_only_one_winner_and_detects_conflicts(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    proposed = job(traceparent=TRACEPARENT_A)
    payload = run_payload()
    stored, created = repository.begin_job(proposed, payload)
    retry, retry_created = repository.begin_job(
        job(
            job_id="job-retry",
            resource_id="run-retry",
            traceparent=TRACEPARENT_B,
        ),
        payload,
    )

    assert (stored, created) == (proposed, True)
    assert retry == proposed
    assert retry_created is False
    assert repository.get_job(proposed.job_id).traceparent == TRACEPARENT_A
    with engine.connect() as connection:
        payload_row = connection.execute(select(job_payloads_table)).mappings().one()
    assert payload_row["job_id"] == proposed.job_id
    assert payload_row["payload_digest"] == payload.payload_digest
    assert (
        payload_row["document"]
        == canonical_json_bytes(payload.model_dump(mode="json")).decode()
    )

    with raises(IdempotencyConflictError, match="different request"):
        repository.begin_job(
            job(job_id="job-other", resource_id="run-other", request="changed"),
            payload,
        )
    with raises(ResourceAlreadySubmittedError, match="already submitted"):
        repository.begin_job(
            job(
                job_id="job-resource",
                idempotency_key="request-resource",
                resource_id="run-001",
            ),
            payload,
        )
    with raises(ImmutableRecordConflictError, match="identity conflicts"):
        repository.begin_job(
            job(
                job_id=proposed.job_id,
                idempotency_key="request-identity-conflict",
                resource_id="run-identity-conflict",
            ),
            payload,
        )
    authoritative, authoritative_created = repository.begin_job(
        job(job_id="job-payload", resource_id="run-payload"),
        run_payload(target_name="fake/different"),
    )
    assert (authoritative, authoritative_created) == (proposed, False)


def test_stored_trace_context_is_bounded_and_validated_on_read(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    proposed = job(traceparent=TRACEPARENT_A)
    repository.begin_job(proposed, run_payload())

    with raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            update(jobs_table)
            .where(jobs_table.c.job_id == proposed.job_id)
            .values(traceparent="00-short")
        )

    with engine.begin() as connection:
        connection.execute(
            update(jobs_table)
            .where(jobs_table.c.job_id == proposed.job_id)
            .values(traceparent=TRACEPARENT_A.upper())
        )
    with raises(CorruptRecordError) as captured:
        repository.get_job(proposed.job_id)
    assert TRACEPARENT_A not in str(captured.value)


def test_concurrent_begin_job_has_exactly_one_insert_winner(engine: Engine) -> None:
    barrier = Barrier(2)
    payload = run_payload()

    def submit(record: JobRecord) -> tuple[JobRecord, bool]:
        barrier.wait()
        return SqlAlchemyControlPlaneRepository(engine).begin_job(record, payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                submit,
                (
                    job(job_id="job-a", resource_id="run-a"),
                    job(job_id="job-b", resource_id="run-b"),
                ),
            )
        )

    assert sorted(created for _record, created in results) == [False, True]
    assert results[0][0] == results[1][0]


def test_job_and_private_payload_insert_roll_back_together(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    def reject_payload_insert(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if (
            statement.lstrip()
            .upper()
            .startswith("INSERT INTO CONTROL_PLANE_JOB_PAYLOADS")
        ):
            raise RuntimeError("injected payload insert failure")

    event.listen(engine, "before_cursor_execute", reject_payload_insert)
    try:
        with raises(RuntimeError, match="injected payload insert failure"):
            repository.begin_job(job(), run_payload())
    finally:
        event.remove(engine, "before_cursor_execute", reject_payload_insert)

    with engine.connect() as connection:
        assert connection.execute(select(jobs_table)).first() is None
        assert connection.execute(select(job_payloads_table)).first() is None


def test_job_payload_has_a_hard_four_mib_bound_before_insert(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
    monkeypatch: MonkeyPatch,
) -> None:
    assert control_plane_db._MAX_JOB_PAYLOAD_BYTES == 4 * 1024 * 1024
    monkeypatch.setattr(control_plane_db, "_MAX_JOB_PAYLOAD_BYTES", 16)

    with raises(PayloadTooLargeError, match="size limit"):
        repository.begin_job(job(), run_payload())

    with engine.connect() as connection:
        assert connection.execute(select(jobs_table)).first() is None
        assert connection.execute(select(job_payloads_table)).first() is None


def test_job_payload_kind_digest_and_required_absence_are_validated(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    payload = run_payload()
    proposed = job()
    repository.begin_job(proposed, payload)
    assert (
        repository.get_job_by_idempotency(JobKind.RUN, proposed.idempotency_key)
        == proposed
    )

    comparison = job(
        job_id="job-comparison-kind",
        kind=JobKind.COMPARISON,
        idempotency_key="request-comparison-kind",
        resource_id="decision-comparison-kind",
    )
    with raises(ValueError, match="kind does not match"):
        repository.begin_job(comparison, payload)

    with engine.begin() as connection:
        connection.execute(
            update(job_payloads_table)
            .where(job_payloads_table.c.job_id == proposed.job_id)
            .values(payload_digest=f"sha256:{'0' * 64}")
        )
    with raises(CorruptRecordError, match="payload is invalid"):
        repository.get_job_by_idempotency(JobKind.RUN, proposed.idempotency_key)
    with raises(CorruptRecordError, match="payload is invalid"):
        repository.begin_job(
            job(job_id="job-retry", resource_id="run-retry"),
            payload,
        )

    with engine.begin() as connection:
        connection.execute(
            delete(job_payloads_table).where(
                job_payloads_table.c.job_id == proposed.job_id
            )
        )
    with raises(CorruptRecordError, match="no worker payload"):
        repository.get_job_by_idempotency(JobKind.RUN, proposed.idempotency_key)
    with raises(CorruptRecordError, match="no worker payload"):
        repository.begin_job(
            job(job_id="job-retry-2", resource_id="run-retry-2"),
            payload,
        )


def test_only_fixed_migration_terminal_shape_may_lack_a_payload(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    migrated = JobRecord(
        job_id="job-migrated",
        kind=JobKind.RUN,
        status=JobStatus.FAILED,
        idempotency_key="request-migrated",
        request_digest=sha256_digest("same"),
        resource_id="run-migrated",
        attempt_count=1,
        max_attempts=3,
        available_at=NOW,
        error_code="legacy_payload_missing",
        created_at=NOW,
        updated_at=NOW,
    )
    with engine.begin() as connection:
        connection.execute(
            insert(jobs_table).values(
                job_id=migrated.job_id,
                kind=migrated.kind.value,
                status=migrated.status.value,
                idempotency_key=migrated.idempotency_key,
                request_digest=migrated.request_digest,
                resource_id=migrated.resource_id,
                attempt_count=migrated.attempt_count,
                max_attempts=migrated.max_attempts,
                available_at=migrated.available_at,
                error_code=migrated.error_code,
                created_at=migrated.created_at,
                updated_at=migrated.updated_at,
                version=1,
            )
        )
        connection.execute(
            insert(job_attempts_table).values(
                job_id=migrated.job_id,
                attempt_number=1,
                status=JobAttemptStatus.FAILED.value,
                worker_id=control_plane_db._MIGRATION_WORKER_ID,
                lease_token=control_plane_db._MIGRATION_LEASE_TOKEN,
                error_code=migrated.error_code,
                started_at=NOW,
                heartbeat_at=NOW,
                lease_expires_at=NOW + timedelta(seconds=1),
                finished_at=NOW,
            )
        )

    assert (
        repository.get_job_by_idempotency(JobKind.RUN, migrated.idempotency_key)
        == migrated
    )
    replay, created = repository.begin_job(
        job(
            job_id="job-migrated-retry",
            idempotency_key=migrated.idempotency_key,
            resource_id="run-migrated-retry",
        ),
        run_payload(),
    )
    assert (replay, created) == (migrated, False)


def test_new_terminal_jobs_with_deleted_payloads_fail_closed(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    canceled_job = job(
        job_id="job-new-canceled",
        idempotency_key="request-new-canceled",
        resource_id="run-new-canceled",
    )
    repository.begin_job(canceled_job, run_payload(data))
    repository.cancel_job(canceled_job.job_id)

    succeeded_job = job(
        job_id="job-new-succeeded",
        idempotency_key="request-new-succeeded",
        resource_id="run-new-succeeded",
    )
    activate_job(engine, repository, succeeded_job, run_payload(data))
    repository.complete_run(
        succeeded_job.job_id,
        RunRecord(
            result=execute(data, run_id=succeeded_job.resource_id),
            created_at=NOW,
        ),
        attempt_number=1,
        lease_token=LEASE_TOKEN_A,
    )

    failed_job = job(
        job_id="job-new-failed",
        idempotency_key="request-new-failed",
        resource_id="run-new-failed",
    )
    activate_job(engine, repository, failed_job, run_payload(data))
    repository.fail_job(
        failed_job.job_id,
        1,
        LEASE_TOKEN_A,
        error_code="execution_failed",
    )

    with engine.begin() as connection:
        connection.execute(
            delete(job_payloads_table).where(
                job_payloads_table.c.job_id.in_(
                    (canceled_job.job_id, succeeded_job.job_id, failed_job.job_id)
                )
            )
        )

    for terminal in (canceled_job, succeeded_job, failed_job):
        with raises(CorruptRecordError, match="no worker payload"):
            repository.get_job_by_idempotency(
                terminal.kind,
                terminal.idempotency_key,
            )


def test_private_payload_read_path_rejects_canonical_and_index_corruption(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
    monkeypatch: MonkeyPatch,
) -> None:
    payload = run_payload()
    records = tuple(
        job(
            job_id=f"job-payload-{label}",
            idempotency_key=f"request-payload-{label}",
            resource_id=f"run-payload-{label}",
        )
        for label in ("text", "canonical", "time", "kind", "size")
    )
    for record in records:
        repository.begin_job(record, payload)
    with engine.connect() as connection:
        document = connection.execute(
            select(job_payloads_table.c.document).where(
                job_payloads_table.c.job_id == records[0].job_id
            )
        ).scalar_one()
    assert isinstance(document, str)

    with engine.begin() as connection:
        connection.execute(
            update(job_payloads_table)
            .where(job_payloads_table.c.job_id == records[0].job_id)
            .values(document=b"not-text")
        )
        connection.execute(
            update(job_payloads_table)
            .where(job_payloads_table.c.job_id == records[1].job_id)
            .values(document=f" {document}")
        )
        connection.execute(
            update(job_payloads_table)
            .where(job_payloads_table.c.job_id == records[2].job_id)
            .values(created_at=NOW + timedelta(seconds=1))
        )

    data = dataset()
    baseline = execute(data, run_id="payload-baseline", target_revision=1)
    candidate = execute(data, run_id="payload-candidate", target_revision=2)
    wrong_kind = comparison_payload(data, baseline, candidate)
    wrong_document = canonical_json_bytes(wrong_kind.model_dump(mode="json")).decode()
    with engine.begin() as connection:
        connection.execute(
            update(job_payloads_table)
            .where(job_payloads_table.c.job_id == records[3].job_id)
            .values(
                document=wrong_document,
                payload_digest=wrong_kind.payload_digest,
            )
        )

    for record in records[:4]:
        with raises(CorruptRecordError, match="payload is invalid"):
            repository.get_job_by_idempotency(record.kind, record.idempotency_key)

    monkeypatch.setattr(
        control_plane_db,
        "_MAX_JOB_PAYLOAD_BYTES",
        len(document.encode()) - 1,
    )
    with raises(CorruptRecordError, match="payload is invalid"):
        repository.get_job_by_idempotency(
            records[4].kind,
            records[4].idempotency_key,
        )


def test_queued_cancellation_is_immediate_idempotent_and_filterable(
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    proposed = job()
    repository.begin_job(proposed, run_payload())

    canceled = repository.cancel_job(proposed.job_id)

    assert canceled.status is JobStatus.CANCELED
    assert repository.cancel_job(proposed.job_id) == canceled
    assert repository.list_jobs(limit=10, status=JobStatus.CANCELED).items == (
        canceled,
    )
    assert repository.list_job_attempts(proposed.job_id) == ()


def test_complete_run_is_atomic_idempotent_and_result_digest_is_not_unique(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    first_result = execute(data, run_id="run-001")
    record = RunRecord(result=first_result, created_at=NOW + timedelta(seconds=2))
    proposed = job()
    activate_job(engine, repository, proposed, run_payload(data))

    completed = repository.complete_run(
        proposed.job_id,
        record,
        attempt_number=1,
        lease_token=LEASE_TOKEN_A,
    )
    retried = repository.complete_run(
        proposed.job_id,
        record,
        attempt_number=1,
        lease_token=LEASE_TOKEN_A,
    )
    assert completed.status is JobStatus.SUCCEEDED
    assert retried == completed
    assert repository.get_run("run-001") == record
    assert repository.list_job_attempts(proposed.job_id)[0].status is (
        JobAttemptStatus.SUCCEEDED
    )
    with raises(LeaseLostError, match="no longer active"):
        repository.complete_run(
            proposed.job_id,
            record,
            attempt_number=1,
            lease_token=LEASE_TOKEN_B,
        )

    second_result = first_result.model_copy(update={"run_id": "run-002"})
    second = RunRecord(result=second_result, created_at=NOW + timedelta(seconds=4))
    repository.put_run(second)
    assert second.result.result_digest == first_result.result_digest


def test_completion_failure_rolls_back_evidence_insert(
    engine: Engine, repository: SqlAlchemyControlPlaneRepository
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    proposed = job()
    activate_job(engine, repository, proposed, run_payload(data))
    record = RunRecord(
        result=execute(data, run_id="run-001"),
        created_at=NOW + timedelta(seconds=2),
    )

    def fail_job_update(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if statement.lstrip().upper().startswith("UPDATE CONTROL_PLANE_JOBS"):
            raise RuntimeError("injected transaction failure")

    event.listen(engine, "before_cursor_execute", fail_job_update)
    try:
        with raises(RuntimeError, match="injected transaction failure"):
            repository.complete_run(
                proposed.job_id,
                record,
                attempt_number=1,
                lease_token=LEASE_TOKEN_A,
            )
    finally:
        event.remove(engine, "before_cursor_execute", fail_job_update)

    assert repository.get_job(proposed.job_id).status is JobStatus.RUNNING
    with raises(RecordNotFoundError, match="not found"):
        repository.get_run(record.run_id)


def test_release_decision_completion_is_append_only(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    baseline = execute(data, run_id="baseline", target_revision=1)
    candidate = execute(data, run_id="candidate", target_revision=2)
    repository.put_run(RunRecord(result=baseline, created_at=NOW))
    repository.put_run(
        RunRecord(result=candidate, created_at=NOW + timedelta(seconds=1))
    )
    evidence = ReleaseDecisionRecord(
        decision_id="decision-001",
        decision=decision(data, baseline, candidate),
        created_at=NOW + timedelta(seconds=3),
    )
    proposed = job(
        job_id="job-decision",
        kind=JobKind.COMPARISON,
        idempotency_key="compare-001",
        resource_id="decision-001",
    )
    activate_job(
        engine,
        repository,
        proposed,
        comparison_payload(data, baseline, candidate),
    )

    completed = repository.complete_release_decision(
        proposed.job_id,
        evidence,
        attempt_number=1,
        lease_token=LEASE_TOKEN_A,
    )

    assert completed.status is JobStatus.SUCCEEDED
    assert repository.get_release_decision("decision-001") == evidence


def test_keyset_pagination_binds_cursor_to_filters_and_rejects_tampering(
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    for index in range(3):
        repository.put_dataset(
            DatasetRecord(
                dataset=dataset(name="fixture", revision=index + 1),
                created_at=NOW,
            )
        )

    first = repository.list_datasets(limit=2, name="fixture")
    assert [record.revision for record in first.items] == [1, 2]
    assert first.next_cursor is not None
    second = repository.list_datasets(limit=2, cursor=first.next_cursor, name="fixture")
    assert [record.revision for record in second.items] == [3]

    with raises(InvalidCursorError, match="invalid"):
        repository.list_datasets(limit=2, cursor=first.next_cursor, name=None)
    with raises(InvalidCursorError, match="invalid"):
        repository.list_datasets(
            limit=2,
            cursor=f"{first.next_cursor[:-1]}A",
            name="fixture",
        )


@mark.parametrize(
    "timestamp",
    (
        "0001-01-01T00:00:00+23:59",
        "9999-12-31T23:59:59-23:59",
    ),
)
def test_cursor_datetime_boundaries_fail_with_stable_error(
    repository: SqlAlchemyControlPlaneRepository,
    timestamp: str,
) -> None:
    cursor = _encode_cursor(
        stream="datasets",
        filters={"name": None},
        key=[timestamp, "fixture", 1],
    )

    with raises(InvalidCursorError, match="invalid"):
        repository.list_datasets(limit=2, cursor=cursor)


def test_stored_document_must_remain_canonical_and_digest_valid(
    engine: Engine, repository: SqlAlchemyControlPlaneRepository
) -> None:
    record = DatasetRecord(dataset=dataset(), created_at=NOW)
    repository.put_dataset(record)
    with engine.begin() as connection:
        connection.execute(
            update(datasets_table)
            .where(datasets_table.c.name == "fixture")
            .values(document='{"revision":1}')
        )

    with raises(CorruptRecordError, match="invalid"):
        repository.get_dataset("fixture", 1)


def test_get_missing_records_has_safe_stable_error(
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    with raises(RecordNotFoundError, match="not found") as captured:
        repository.get_run("private-run-id")

    assert "private-run-id" not in str(captured.value)
    with raises(ValueError, match="between 1 and 100"):
        repository.list_runs(limit=0)


@mark.parametrize(
    ("column", "tampered", "lookup_name", "lookup_revision"),
    (
        ("name", "tampered", "tampered", 1),
        ("revision", 9, "fixture", 9),
        ("digest", f"sha256:{'0' * 64}", "fixture", 1),
    ),
)
def test_dataset_row_indexes_must_match_canonical_document(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
    column: str,
    tampered: object,
    lookup_name: str,
    lookup_revision: int,
) -> None:
    repository.put_dataset(DatasetRecord(dataset=dataset(), created_at=NOW))
    with engine.begin() as connection:
        connection.execute(
            update(datasets_table)
            .where(datasets_table.c.name == "fixture")
            .values(**{column: tampered})
        )

    with raises(CorruptRecordError, match="indexes"):
        repository.get_dataset(lookup_name, lookup_revision)


@mark.parametrize(
    ("column", "tampered", "lookup_run_id"),
    (
        ("run_id", "run-tampered", "run-tampered"),
        ("result_digest", f"sha256:{'0' * 64}", "run-001"),
        ("dataset_name", "fixture-other", "run-001"),
        ("dataset_revision", 2, "run-001"),
    ),
)
def test_run_row_indexes_must_match_canonical_document(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
    column: str,
    tampered: object,
    lookup_run_id: str,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    repository.put_dataset(DatasetRecord(dataset=dataset(revision=2), created_at=NOW))
    repository.put_dataset(
        DatasetRecord(dataset=dataset(name="fixture-other"), created_at=NOW)
    )
    repository.put_run(
        RunRecord(result=execute(data, run_id="run-001"), created_at=NOW)
    )
    with engine.begin() as connection:
        connection.execute(
            update(runs_table)
            .where(runs_table.c.run_id == "run-001")
            .values(**{column: tampered})
        )

    with raises(CorruptRecordError, match="indexes"):
        repository.get_run(lookup_run_id)


@mark.parametrize(
    ("column", "tampered"),
    (
        ("decision_digest", f"sha256:{'0' * 64}"),
        ("baseline_run_id", "candidate"),
        ("candidate_run_id", "baseline"),
        ("status", "failed"),
    ),
)
def test_decision_row_indexes_must_match_canonical_document(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
    column: str,
    tampered: object,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    baseline = execute(data, run_id="baseline", target_revision=1)
    candidate = execute(data, run_id="candidate", target_revision=2)
    repository.put_run(RunRecord(result=baseline, created_at=NOW))
    repository.put_run(RunRecord(result=candidate, created_at=NOW))
    repository.put_release_decision(
        ReleaseDecisionRecord(
            decision_id="decision-001",
            decision=decision(data, baseline, candidate),
            created_at=NOW,
        )
    )
    with engine.begin() as connection:
        connection.execute(
            update(release_decisions_table)
            .where(release_decisions_table.c.decision_id == "decision-001")
            .values(**{column: tampered})
        )

    with raises(CorruptRecordError, match="indexes"):
        repository.get_release_decision("decision-001")


def test_valid_canonical_document_cannot_be_swapped_under_another_key(
    engine: Engine, repository: SqlAlchemyControlPlaneRepository
) -> None:
    first = DatasetRecord(dataset=dataset(name="first"), created_at=NOW)
    second = DatasetRecord(dataset=dataset(name="second"), created_at=NOW)
    repository.put_dataset(first)
    repository.put_dataset(second)
    with engine.begin() as connection:
        first_document = connection.execute(
            select(datasets_table.c.document).where(datasets_table.c.name == "first")
        ).scalar_one()
        connection.execute(
            update(datasets_table)
            .where(datasets_table.c.name == "second")
            .values(document=first_document)
        )

    with raises(CorruptRecordError, match="indexes"):
        repository.get_dataset("second", 1)


def _encoded_bytes(value: object) -> str:
    return base64.urlsafe_b64encode(canonical_json_bytes(value)).decode().rstrip("=")


def test_cursor_decoder_rejects_malformed_envelopes_keys_and_timestamps(
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    payload = {
        "filters": {"name": None},
        "key": [NOW.isoformat(), "fixture", 1],
        "stream": "datasets",
        "version": 1,
    }
    malformed = (
        "",
        "!",
        base64.urlsafe_b64encode(b" []").decode().rstrip("="),
        _encoded_bytes([]),
        _encoded_bytes({"checksum": "bad", "payload": []}),
        _encoded_bytes({"checksum": "bad", "payload": payload}),
        _encode_cursor(
            stream="datasets",
            filters={"name": None},
            key=[],
        ),
        _encode_cursor(
            stream="datasets",
            filters={"name": None},
            key=[1, "fixture", 1],
        ),
        _encode_cursor(
            stream="datasets",
            filters={"name": None},
            key=["2026-08-20T12:00:00", "fixture", 1],
        ),
    )

    for cursor in malformed:
        with raises(InvalidCursorError, match="invalid"):
            repository.list_datasets(limit=1, cursor=cursor)


def test_job_run_and_decision_lists_use_filter_bound_keyset_cursors(
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    results: list[RunResult] = []
    for index in range(3):
        created_at = NOW + timedelta(seconds=index)
        proposed = job(
            job_id=f"job-{index}",
            idempotency_key=f"request-{index}",
            resource_id=f"run-{index}",
            created_at=created_at,
        )
        repository.begin_job(
            proposed,
            run_payload(data, target_revision=index + 1),
        )
        result = execute(
            data,
            run_id=f"run-{index}",
            target_revision=index + 1,
        )
        results.append(result)
        repository.put_run(RunRecord(result=result, created_at=created_at))

    release = decision(data, results[0], results[1])
    for index in range(3):
        repository.put_release_decision(
            ReleaseDecisionRecord(
                decision_id=f"decision-{index}",
                decision=release,
                created_at=NOW + timedelta(seconds=index),
            )
        )

    jobs = repository.list_jobs(
        limit=2,
        kind=JobKind.RUN,
        status=JobStatus.QUEUED,
    )
    assert [item.job_id for item in jobs.items] == ["job-0", "job-1"]
    assert jobs.next_cursor is not None
    remaining_jobs = repository.list_jobs(
        limit=2,
        cursor=jobs.next_cursor,
        kind=JobKind.RUN,
        status=JobStatus.QUEUED,
    )
    assert [item.job_id for item in remaining_jobs.items] == ["job-2"]

    runs = repository.list_runs(limit=2, dataset_name="fixture")
    assert [item.run_id for item in runs.items] == ["run-0", "run-1"]
    assert runs.next_cursor is not None
    remaining_runs = repository.list_runs(
        limit=2,
        cursor=runs.next_cursor,
        dataset_name="fixture",
    )
    assert [item.run_id for item in remaining_runs.items] == ["run-2"]

    decisions = repository.list_release_decisions(
        limit=2,
        status=ReleaseStatus.PASSED,
    )
    assert [item.decision_id for item in decisions.items] == [
        "decision-0",
        "decision-1",
    ]
    assert decisions.next_cursor is not None
    remaining_decisions = repository.list_release_decisions(
        limit=2,
        cursor=decisions.next_cursor,
        status=ReleaseStatus.PASSED,
    )
    assert [item.decision_id for item in remaining_decisions.items] == ["decision-2"]
    assert (
        repository.list_release_decisions(
            limit=10,
            status=ReleaseStatus.FAILED,
        ).items
        == ()
    )

    same = ReleaseDecisionRecord(
        decision_id="decision-0",
        decision=release,
        created_at=NOW,
    )
    assert repository.put_release_decision(same) == same
    different = ReleaseDecisionRecord(
        decision_id="decision-0",
        decision=decision(data, results[0], results[2]),
        created_at=NOW,
    )
    with raises(ImmutableRecordConflictError, match="different evidence"):
        repository.put_release_decision(different)


def test_collection_projections_do_not_load_canonical_evidence_documents(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    baseline = execute(data, run_id="baseline-projection", target_revision=1)
    candidate = execute(data, run_id="candidate-projection", target_revision=2)
    repository.put_run(RunRecord(result=baseline, created_at=NOW))
    repository.put_run(RunRecord(result=candidate, created_at=NOW))
    release = ReleaseDecisionRecord(
        decision_id="decision-projection",
        decision=decision(data, baseline, candidate),
        created_at=NOW,
    )
    repository.put_release_decision(release)

    poison = "not-canonical-json-" * 65_536
    with engine.begin() as connection:
        connection.execute(update(datasets_table).values(document=poison))
        connection.execute(update(runs_table).values(document=poison))
        connection.execute(update(release_decisions_table).values(document=poison))

    statements: list[str] = []

    def capture_selects(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_selects)
    try:
        assert repository.list_datasets(limit=10).items[0].case_count == 1
        assert repository.list_runs(limit=10).items[0].status.value == "completed"
        assert (
            repository.list_release_decisions(limit=10).items[0].decision_id
            == "decision-projection"
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_selects)

    assert len(statements) == 3
    assert all("document" not in statement.lower() for statement in statements)
    with raises(CorruptRecordError, match="invalid"):
        repository.get_dataset("fixture", 1)
    with raises(CorruptRecordError, match="invalid"):
        repository.get_run("baseline-projection")
    with raises(CorruptRecordError, match="invalid"):
        repository.get_release_decision("decision-projection")


def test_unavailable_database_operations_raise_sanitized_adapter_errors(
    tmp_path: Path,
) -> None:
    unavailable_engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'missing-parent' / 'database.sqlite3'}"
    )
    unavailable = SqlAlchemyControlPlaneRepository(unavailable_engine)
    try:
        with raises(ControlPlaneRepositoryError, match="unavailable") as captured:
            unavailable.check_health()
        assert "missing-parent" not in str(captured.value)
    finally:
        unavailable_engine.dispose()

    empty_engine = create_engine("sqlite+pysqlite:///:memory:")
    empty = SqlAlchemyControlPlaneRepository(empty_engine)
    data = dataset()
    run_record = RunRecord(result=execute(data, run_id="private-run"), created_at=NOW)
    baseline = execute(data, run_id="private-baseline", target_revision=1)
    candidate = execute(data, run_id="private-candidate", target_revision=2)
    decision_record = ReleaseDecisionRecord(
        decision_id="private-decision",
        decision=decision(data, baseline, candidate),
        created_at=NOW,
    )
    operations: tuple[tuple[str, Callable[[], object]], ...] = (
        (
            "store dataset revision",
            lambda: empty.put_dataset(DatasetRecord(dataset=data, created_at=NOW)),
        ),
        ("load dataset revision", lambda: empty.get_dataset("private-dataset", 1)),
        ("list dataset revisions", lambda: empty.list_datasets(limit=1)),
        ("submit job", lambda: empty.begin_job(job(), run_payload(data))),
        ("load job", lambda: empty.get_job("private-job")),
        ("list jobs", lambda: empty.list_jobs(limit=1)),
        ("cancel job", lambda: empty.cancel_job("private-job")),
        ("list job attempts", lambda: empty.list_job_attempts("private-job")),
        (
            "heartbeat worker lease",
            lambda: empty.heartbeat_job(
                "private-job",
                1,
                LEASE_TOKEN_A,
                lease_seconds=30,
            ),
        ),
        (
            "schedule job retry",
            lambda: empty.retry_job(
                "private-job",
                1,
                LEASE_TOKEN_A,
                error_code="retryable_error",
                delay_seconds=1,
            ),
        ),
        (
            "fail job",
            lambda: empty.fail_job(
                "private-job",
                1,
                LEASE_TOKEN_A,
                error_code="terminal_error",
            ),
        ),
        (
            "acknowledge job cancellation",
            lambda: empty.acknowledge_cancellation(
                "private-job",
                1,
                LEASE_TOKEN_A,
            ),
        ),
        (
            "complete run job",
            lambda: empty.complete_run(
                "private-job",
                run_record,
                attempt_number=1,
                lease_token=LEASE_TOKEN_A,
            ),
        ),
        (
            "complete comparison job",
            lambda: empty.complete_release_decision(
                "private-job",
                decision_record,
                attempt_number=1,
                lease_token=LEASE_TOKEN_A,
            ),
        ),
        ("store run evidence", lambda: empty.put_run(run_record)),
        ("load run evidence", lambda: empty.get_run("private-run")),
        ("list runs", lambda: empty.list_runs(limit=1)),
        (
            "store release decision",
            lambda: empty.put_release_decision(decision_record),
        ),
        (
            "load release decision",
            lambda: empty.get_release_decision("private-decision"),
        ),
        (
            "list release decisions",
            lambda: empty.list_release_decisions(limit=1),
        ),
    )
    try:
        assert empty.schema_is_current() is False
        for message, operation in operations:
            with raises(ControlPlaneRepositoryError, match=message) as captured:
                operation()
            assert "private" not in str(captured.value)
    finally:
        empty_engine.dispose()


def test_corrupt_documents_and_rows_fail_closed(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    dataset_record = DatasetRecord(dataset=data, created_at=NOW)
    repository.put_dataset(dataset_record)
    with engine.connect() as connection:
        dataset_row = dict(connection.execute(select(datasets_table)).mappings().one())
    document = cast(str, dataset_row["document"])

    bounded_reader = SqlAlchemyControlPlaneRepository(
        engine,
        max_document_bytes=len(document.encode()) - 1,
    )
    with raises(CorruptRecordError, match="invalid"):
        bounded_reader.get_dataset("fixture", 1)

    with engine.begin() as connection:
        connection.execute(update(datasets_table).values(document=f" {document}"))
    with raises(CorruptRecordError, match="invalid"):
        repository.get_dataset("fixture", 1)
    with engine.begin() as connection:
        connection.execute(update(datasets_table).values(document=document))

    non_text = {**dataset_row, "document": b"not-text"}
    with raises(CorruptRecordError, match="invalid"):
        repository._dataset_record(cast(RowMapping, non_text))
    missing_dataset_time = dict(dataset_row)
    missing_dataset_time.pop("created_at")
    with raises(CorruptRecordError, match="dataset is invalid"):
        repository._dataset_record(cast(RowMapping, missing_dataset_time))
    mismatched_case_count = {**dataset_row, "case_count": 2}
    with raises(CorruptRecordError, match="indexes"):
        repository._dataset_record(cast(RowMapping, mismatched_case_count))
    invalid_dataset_projection = {**dataset_row, "case_count": 0}
    with raises(CorruptRecordError, match="projection is invalid"):
        repository._dataset_list_record(cast(RowMapping, invalid_dataset_projection))

    with raises(CorruptRecordError, match="timestamp"):
        _aware("not-a-timestamp")
    assert _aware(NOW) == NOW

    proposed = job()
    repository.begin_job(proposed, run_payload(data))
    with engine.connect() as connection:
        job_row = dict(connection.execute(select(jobs_table)).mappings().one())
    invalid_job = {**job_row, "status": "not-a-status"}
    with raises(CorruptRecordError, match="job is invalid"):
        repository._job_record(cast(RowMapping, invalid_job))

    run_record = RunRecord(result=execute(data, run_id="run-corrupt"), created_at=NOW)
    repository.put_run(run_record)
    with engine.connect() as connection:
        run_row = dict(connection.execute(select(runs_table)).mappings().one())
    missing_run_time = dict(run_row)
    missing_run_time.pop("created_at")
    with raises(CorruptRecordError, match="run is invalid"):
        repository._run_record(cast(RowMapping, missing_run_time))
    mismatched_run_status = {**run_row, "status": "completed_with_failures"}
    with raises(CorruptRecordError, match="indexes"):
        repository._run_record(cast(RowMapping, mismatched_run_status))
    mismatched_run_mode = {**run_row, "execution_mode": "live"}
    with raises(CorruptRecordError, match="indexes"):
        repository._run_record(cast(RowMapping, mismatched_run_mode))
    invalid_run_projection = {**run_row, "status": "not-a-status"}
    with raises(CorruptRecordError, match="projection is invalid"):
        repository._run_list_record(cast(RowMapping, invalid_run_projection))

    baseline = execute(data, run_id="baseline-corrupt", target_revision=1)
    candidate = execute(data, run_id="candidate-corrupt", target_revision=2)
    repository.put_run(RunRecord(result=baseline, created_at=NOW))
    repository.put_run(RunRecord(result=candidate, created_at=NOW))
    release = ReleaseDecisionRecord(
        decision_id="decision-corrupt",
        decision=decision(data, baseline, candidate),
        created_at=NOW,
    )
    repository.put_release_decision(release)
    with engine.connect() as connection:
        decision_row = dict(
            connection.execute(select(release_decisions_table)).mappings().one()
        )
    missing_decision_id = dict(decision_row)
    missing_decision_id.pop("decision_id")
    with raises(CorruptRecordError, match="decision is invalid"):
        repository._release_decision_record(cast(RowMapping, missing_decision_id))
    invalid_decision_projection = {**decision_row, "decision_digest": "invalid"}
    with raises(CorruptRecordError, match="projection is invalid"):
        repository._release_decision_list_record(
            cast(RowMapping, invalid_decision_projection)
        )


def test_repository_rejects_invalid_limits_and_nonqueued_job_claims(
    engine: Engine,
) -> None:
    for bound in (0, True):
        with raises(ValueError, match="positive integer"):
            SqlAlchemyControlPlaneRepository(engine, max_document_bytes=bound)

    repository = SqlAlchemyControlPlaneRepository(engine)
    running = job().model_copy(update={"status": JobStatus.RUNNING})
    with raises(ValueError, match="unattempted queued job"):
        repository.begin_job(running, run_payload())

    for limit in (True, 101):
        with raises(ValueError, match="between 1 and 100"):
            repository.list_jobs(limit=limit)


def test_sqlite_rejects_claim_and_reaper_coordination_explicitly(
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    with raises(ControlPlaneRepositoryError, match="requires PostgreSQL"):
        repository.claim_next_job(
            worker_id="worker-a",
            lease_token=LEASE_TOKEN_A,
            lease_seconds=30,
        )
    with raises(ControlPlaneRepositoryError, match="requires PostgreSQL"):
        repository.reap_expired_jobs(
            limit=10,
            retry_base_seconds=2,
            retry_max_seconds=60,
        )

    with raises(ValueError, match="supported range"):
        repository.claim_next_job(
            worker_id="worker-a",
            lease_token=LEASE_TOKEN_A,
            lease_seconds=4,
        )
    with raises(ValueError, match="cannot be below"):
        repository.reap_expired_jobs(
            limit=10,
            retry_base_seconds=10,
            retry_max_seconds=5,
        )


def test_private_coordination_sql_failures_are_sanitized_on_sqlite(
    monkeypatch: MonkeyPatch,
) -> None:
    empty_engine = create_engine("sqlite+pysqlite:///:memory:")
    repository = SqlAlchemyControlPlaneRepository(empty_engine)
    monkeypatch.setattr(repository, "_require_postgresql_coordination", lambda: None)
    try:
        with raises(ControlPlaneRepositoryError, match="claim queued job") as claim:
            repository.claim_next_job(
                worker_id="worker-portable",
                lease_token=LEASE_TOKEN_A,
                lease_seconds=30,
            )
        with raises(ControlPlaneRepositoryError, match="recover expired jobs") as reap:
            repository.reap_expired_jobs(
                limit=10,
                retry_base_seconds=2,
                retry_max_seconds=60,
            )
        assert "control_plane_jobs" not in str(claim.value)
        assert "control_plane_jobs" not in str(reap.value)
    finally:
        empty_engine.dispose()


def test_missing_records_and_corrupt_private_lease_state_fail_closed(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    with raises(RecordNotFoundError, match="not found"):
        repository.get_dataset("missing-dataset", 1)
    with raises(RecordNotFoundError, match="not found"):
        repository.get_release_decision("missing-decision")
    with raises(LeaseLostError, match="no longer active"):
        repository.heartbeat_job(
            "job-missing",
            1,
            LEASE_TOKEN_A,
            lease_seconds=30,
        )

    incomplete = job(
        job_id="job-missing-attempt",
        idempotency_key="request-missing-attempt",
        resource_id="run-missing-attempt",
    )
    repository.begin_job(incomplete, run_payload())
    running = incomplete.transition_to(
        JobStatus.RUNNING,
        at=incomplete.updated_at + timedelta(seconds=1),
    )
    with engine.begin() as connection:
        connection.execute(
            update(jobs_table)
            .where(jobs_table.c.job_id == incomplete.job_id)
            .values(
                status=running.status.value,
                attempt_count=running.attempt_count,
                updated_at=running.updated_at,
                version=1,
            )
        )
    with raises(CorruptRecordError, match="no active attempt"):
        repository.cancel_job(incomplete.job_id)
    with raises(LeaseLostError, match="no longer active"):
        repository.heartbeat_job(
            incomplete.job_id,
            1,
            LEASE_TOKEN_A,
            lease_seconds=30,
        )

    terminal = job(
        job_id="job-terminal-without-attempt",
        idempotency_key="request-terminal-without-attempt",
        resource_id="run-terminal-without-attempt",
    )
    repository.begin_job(terminal, run_payload())
    terminal_running = terminal.transition_to(
        JobStatus.RUNNING,
        at=terminal.updated_at + timedelta(seconds=1),
    )
    failed = terminal_running.transition_to(
        JobStatus.FAILED,
        at=terminal_running.updated_at + timedelta(seconds=1),
        error_code="legacy_payload_missing",
    )
    with engine.begin() as connection:
        connection.execute(
            update(jobs_table)
            .where(jobs_table.c.job_id == terminal.job_id)
            .values(
                status=failed.status.value,
                attempt_count=failed.attempt_count,
                error_code=failed.error_code,
                updated_at=failed.updated_at,
                version=1,
            )
        )
        connection.execute(
            delete(job_payloads_table).where(
                job_payloads_table.c.job_id == terminal.job_id
            )
        )
    with raises(CorruptRecordError, match="no worker payload"):
        repository.get_job_by_idempotency(terminal.kind, terminal.idempotency_key)

    invalid_attempt = {
        "job_id": "job-corrupt-attempt",
        "attempt_number": 1,
        "status": JobAttemptStatus.RUNNING.value,
        "worker_id": "private worker identity",
        "lease_token": LEASE_TOKEN_A,
        "error_code": None,
        "started_at": NOW,
        "heartbeat_at": NOW,
        "lease_expires_at": NOW + timedelta(seconds=30),
        "finished_at": None,
    }
    with raises(CorruptRecordError, match="attempt is invalid") as captured:
        repository._attempt_record(invalid_attempt)
    assert "private worker identity" not in str(captured.value)


def test_claim_transaction_algorithm_is_portable_in_one_sqlite_connection(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository, "_require_postgresql_coordination", lambda: None)
    payload = run_payload()
    eligible = job()
    future_time = datetime.now(UTC) + timedelta(hours=1)
    future = job(
        job_id="job-future",
        idempotency_key="request-future",
        resource_id="run-future",
        created_at=future_time,
    )
    repository.begin_job(eligible, payload)
    repository.begin_job(future, payload)

    claim = repository.claim_next_job(
        worker_id="worker-portable",
        lease_token=LEASE_TOKEN_A,
        lease_seconds=30,
    )

    assert claim is not None
    assert claim.job.job_id == eligible.job_id
    assert claim.payload == payload
    assert claim.attempt.attempt_number == 1
    assert claim.attempt.lease_expires_at - claim.attempt.heartbeat_at == timedelta(
        seconds=30
    )
    assert (
        repository.claim_next_job(
            worker_id="worker-portable",
            lease_token=LEASE_TOKEN_B,
            lease_seconds=30,
        )
        is None
    )

    contender = job(
        job_id="job-claim-conflict",
        idempotency_key="request-claim-conflict",
        resource_id="run-claim-conflict",
    )
    repository.begin_job(contender, payload)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER reject_claim_attempt "
                "BEFORE INSERT ON control_plane_job_attempts "
                "BEGIN SELECT RAISE(ABORT, 'private trigger detail'); END"
            )
        )
    with raises(ConcurrentTransitionError, match="conflicted") as captured:
        repository.claim_next_job(
            worker_id="worker-portable",
            lease_token=LEASE_TOKEN_B,
            lease_seconds=30,
        )
    assert "private trigger detail" not in str(captured.value)
    assert repository.get_job(contender.job_id).status is JobStatus.QUEUED
    assert repository.list_job_attempts(contender.job_id) == ()


def test_reaper_transaction_algorithm_handles_retry_exhaustion_and_cancel(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository, "_require_postgresql_coordination", lambda: None)
    retry_record = job(
        job_id="job-reaper-retry",
        idempotency_key="request-reaper-retry",
        resource_id="run-reaper-retry",
    )
    exhausted_record = job(
        job_id="job-reaper-exhausted",
        idempotency_key="request-reaper-exhausted",
        resource_id="run-reaper-exhausted",
        max_attempts=1,
    )
    canceled_record = job(
        job_id="job-reaper-canceled",
        idempotency_key="request-reaper-canceled",
        resource_id="run-reaper-canceled",
    )
    for record, token in (
        (retry_record, LEASE_TOKEN_A),
        (exhausted_record, LEASE_TOKEN_B),
        (canceled_record, "lease_token_c_0123456789abcdef0123456789abcdef"),
    ):
        activate_job(
            engine,
            repository,
            record,
            run_payload(),
            lease_token=token,
            expired=True,
        )
    requested = repository.cancel_job(canceled_record.job_id)
    assert requested.status is JobStatus.CANCEL_REQUESTED

    recovered = repository.reap_expired_jobs(
        limit=10,
        retry_base_seconds=2,
        retry_max_seconds=3,
    )
    by_id = {record.job_id: record for record in recovered}

    assert by_id[retry_record.job_id].status is JobStatus.QUEUED
    assert (
        by_id[retry_record.job_id].available_at > by_id[retry_record.job_id].updated_at
    )
    assert by_id[exhausted_record.job_id].status is JobStatus.FAILED
    assert by_id[exhausted_record.job_id].error_code == "lease_expired"
    assert by_id[canceled_record.job_id].status is JobStatus.CANCELED
    assert repository.list_job_attempts(retry_record.job_id)[0].status is (
        JobAttemptStatus.LEASE_EXPIRED
    )
    assert repository.list_job_attempts(exhausted_record.job_id)[0].status is (
        JobAttemptStatus.LEASE_EXPIRED
    )
    assert repository.list_job_attempts(canceled_record.job_id)[0].status is (
        JobAttemptStatus.CANCELED
    )
    assert (
        repository.reap_expired_jobs(
            limit=10,
            retry_base_seconds=2,
            retry_max_seconds=3,
        )
        == ()
    )


def test_worker_inputs_fail_before_private_values_reach_storage_errors(
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    with raises(ValueError, match="worker identity is invalid") as worker_error:
        repository.claim_next_job(
            worker_id="private worker identity",
            lease_token=LEASE_TOKEN_A,
            lease_seconds=30,
        )
    assert "private worker identity" not in str(worker_error.value)

    with raises(ValueError, match="lease token is invalid") as token_error:
        repository.heartbeat_job(
            "job-private",
            1,
            "private-token",
            lease_seconds=30,
        )
    assert "private-token" not in str(token_error.value)

    with raises(ValueError, match="error code is invalid") as code_error:
        repository.retry_job(
            "job-private",
            1,
            LEASE_TOKEN_A,
            delay_seconds=5,
            error_code="Private Error",
        )
    assert "Private Error" not in str(code_error.value)


def test_heartbeat_and_attempt_history_are_fenced_and_public_safe(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    proposed = job()
    running = activate_job(engine, repository, proposed, run_payload())

    heartbeat = repository.heartbeat_job(
        proposed.job_id,
        1,
        LEASE_TOKEN_A,
        lease_seconds=30,
    )
    attempt = repository.list_job_attempts(proposed.job_id)[0]

    assert heartbeat == running
    assert attempt.status is JobAttemptStatus.RUNNING
    assert attempt.heartbeat_at >= attempt.started_at
    assert attempt.lease_expires_at > attempt.heartbeat_at
    assert "lease_token" not in attempt.model_dump()
    assert "worker_id" not in attempt.model_dump()
    with raises(LeaseLostError, match="no longer active"):
        repository.heartbeat_job(
            proposed.job_id,
            1,
            LEASE_TOKEN_B,
            lease_seconds=30,
        )


def test_active_lease_samples_database_time_after_both_row_locks(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    proposed = job()
    activate_job(engine, repository, proposed, run_payload())
    statements: list[str] = []

    def capture_statements(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        statements.append(" ".join(statement.upper().split()))

    event.listen(engine, "before_cursor_execute", capture_statements)
    try:
        repository.heartbeat_job(
            proposed.job_id,
            1,
            LEASE_TOKEN_A,
            lease_seconds=30,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statements)

    job_lock = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("SELECT") and "FROM CONTROL_PLANE_JOBS" in statement
    )
    attempt_lock = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("SELECT")
        and "FROM CONTROL_PLANE_JOB_ATTEMPTS" in statement
    )
    sampled_time = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("SELECT CURRENT_TIMESTAMP")
    )
    heartbeat_update = next(
        statement
        for statement in statements
        if statement.startswith("UPDATE CONTROL_PLANE_JOB_ATTEMPTS")
    )
    assert job_lock < attempt_lock < sampled_time
    assert "LEASE_EXPIRES_AT > CURRENT_TIMESTAMP" in heartbeat_update


def test_attempt_history_uses_one_snapshot_and_detects_gaps(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    proposed = job()
    repository.begin_job(proposed, run_payload())
    statements: list[str] = []

    def capture_selects(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_selects)
    try:
        assert repository.list_job_attempts(proposed.job_id) == ()
    finally:
        event.remove(engine, "before_cursor_execute", capture_selects)
    assert len(statements) == 1
    assert "LEFT OUTER JOIN control_plane_job_attempts" in statements[0]

    activate_job(engine, repository, proposed, run_payload())
    assert len(repository.list_job_attempts(proposed.job_id)) == 1
    with engine.begin() as connection:
        connection.execute(
            delete(job_attempts_table).where(
                job_attempts_table.c.job_id == proposed.job_id
            )
        )
    with raises(CorruptRecordError, match="history is incomplete"):
        repository.list_job_attempts(proposed.job_id)
    with raises(RecordNotFoundError, match="not found"):
        repository.list_job_attempts("job-missing")


def test_cancel_acknowledgement_and_terminal_conflicts_cover_public_states(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    with raises(RecordNotFoundError, match="not found"):
        repository.cancel_job("job-missing")
    with raises(LeaseLostError, match="no longer active"):
        repository.acknowledge_cancellation(
            "job-missing",
            1,
            LEASE_TOKEN_A,
        )

    proposed = job()
    activate_job(engine, repository, proposed, run_payload())
    with raises(IllegalJobTransitionError, match="no cancellation request"):
        repository.acknowledge_cancellation(
            proposed.job_id,
            1,
            LEASE_TOKEN_A,
        )
    requested = repository.cancel_job(proposed.job_id)
    assert requested.status is JobStatus.CANCEL_REQUESTED
    assert repository.cancel_job(proposed.job_id) == requested
    canceled = repository.acknowledge_cancellation(
        proposed.job_id,
        1,
        LEASE_TOKEN_A,
    )
    assert canceled.status is JobStatus.CANCELED

    failed_record = job(
        job_id="job-terminal-cancel",
        idempotency_key="request-terminal-cancel",
        resource_id="run-terminal-cancel",
    )
    activate_job(engine, repository, failed_record, run_payload())
    repository.fail_job(
        failed_record.job_id,
        1,
        LEASE_TOKEN_A,
        error_code="execution_failed",
    )
    with raises(IllegalJobTransitionError, match="Terminal job"):
        repository.cancel_job(failed_record.job_id)

    fail_after_cancel = job(
        job_id="job-fail-after-cancel",
        idempotency_key="request-fail-after-cancel",
        resource_id="run-fail-after-cancel",
    )
    activate_job(engine, repository, fail_after_cancel, run_payload())
    repository.cancel_job(fail_after_cancel.job_id)
    canceled_by_failure = repository.fail_job(
        fail_after_cancel.job_id,
        1,
        LEASE_TOKEN_A,
        error_code="execution_failed",
    )
    assert canceled_by_failure.status is JobStatus.CANCELED


def test_retry_at_attempt_limit_and_fenced_update_failures_do_not_mutate(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    limited = job(max_attempts=1)
    activate_job(engine, repository, limited, run_payload())
    with raises(IllegalJobTransitionError, match="another attempt"):
        repository.retry_job(
            limited.job_id,
            1,
            LEASE_TOKEN_A,
            delay_seconds=5,
            error_code="temporary_failure",
        )
    assert repository.get_job(limited.job_id).status is JobStatus.RUNNING

    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER reject_heartbeat "
                "BEFORE UPDATE OF heartbeat_at ON control_plane_job_attempts "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        )
    with raises(LeaseLostError, match="no longer active"):
        repository.heartbeat_job(
            limited.job_id,
            1,
            LEASE_TOKEN_A,
            lease_seconds=30,
        )


def test_fenced_completion_rolls_back_when_attempt_or_job_update_loses(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    attempt_record = job(resource_id="run-attempt-fence")
    activate_job(engine, repository, attempt_record, run_payload(data))
    attempt_evidence = RunRecord(
        result=execute(data, run_id=attempt_record.resource_id),
        created_at=NOW,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER reject_attempt_finish "
                "BEFORE UPDATE OF status ON control_plane_job_attempts "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        )
    with raises(LeaseLostError, match="no longer active"):
        repository.complete_run(
            attempt_record.job_id,
            attempt_evidence,
            attempt_number=1,
            lease_token=LEASE_TOKEN_A,
        )
    with raises(RecordNotFoundError, match="not found"):
        repository.get_run(attempt_evidence.run_id)

    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER reject_attempt_finish"))
    job_record = job(
        job_id="job-update-fence",
        idempotency_key="request-update-fence",
        resource_id="run-update-fence",
    )
    activate_job(engine, repository, job_record, run_payload(data))
    job_evidence = RunRecord(
        result=execute(data, run_id=job_record.resource_id),
        created_at=NOW,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER reject_job_finish "
                "BEFORE UPDATE ON control_plane_jobs "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        )
    with raises(ConcurrentTransitionError, match="changed"):
        repository.complete_run(
            job_record.job_id,
            job_evidence,
            attempt_number=1,
            lease_token=LEASE_TOKEN_A,
        )
    with raises(RecordNotFoundError, match="not found"):
        repository.get_run(job_evidence.run_id)


def test_completion_rejects_missing_and_nonrunning_jobs_before_publication(
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    run_record = RunRecord(
        result=execute(data, run_id="run-queued-completion"),
        created_at=NOW,
    )
    with raises(LeaseLostError, match="no longer active"):
        repository.complete_run(
            "job-missing-completion",
            run_record,
            attempt_number=1,
            lease_token=LEASE_TOKEN_A,
        )

    queued_run = job(
        job_id="job-queued-completion",
        idempotency_key="request-queued-completion",
        resource_id=run_record.run_id,
    )
    repository.begin_job(queued_run, run_payload(data))
    with raises(LeaseLostError, match="no longer active"):
        repository.complete_run(
            queued_run.job_id,
            run_record,
            attempt_number=1,
            lease_token=LEASE_TOKEN_A,
        )

    baseline = execute(data, run_id="baseline-queued-completion", target_revision=1)
    candidate = execute(data, run_id="candidate-queued-completion", target_revision=2)
    decision_record = ReleaseDecisionRecord(
        decision_id="decision-queued-completion",
        decision=decision(data, baseline, candidate),
        created_at=NOW,
    )
    queued_decision = job(
        job_id="job-queued-decision-completion",
        kind=JobKind.COMPARISON,
        idempotency_key="request-queued-decision-completion",
        resource_id=decision_record.decision_id,
    )
    repository.begin_job(
        queued_decision,
        comparison_payload(data, baseline, candidate),
    )
    with raises(LeaseLostError, match="no longer active"):
        repository.complete_release_decision(
            queued_decision.job_id,
            decision_record,
            attempt_number=1,
            lease_token=LEASE_TOKEN_A,
        )

    with raises(RecordNotFoundError, match="not found"):
        repository.get_run(run_record.run_id)
    with raises(RecordNotFoundError, match="not found"):
        repository.get_release_decision(decision_record.decision_id)


def test_preexisting_identical_evidence_and_decision_cancellation_are_atomic(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    baseline = execute(data, run_id="run-preexisting", target_revision=1)
    candidate = execute(data, run_id="candidate-preexisting", target_revision=2)
    run_record = RunRecord(result=baseline, created_at=NOW)
    repository.put_run(run_record)
    repository.put_run(
        RunRecord(result=candidate, created_at=NOW + timedelta(seconds=1))
    )

    run_job = job(
        job_id="job-preexisting-run",
        idempotency_key="request-preexisting-run",
        resource_id=run_record.run_id,
    )
    activate_job(engine, repository, run_job, run_payload(data))
    assert (
        repository.complete_run(
            run_job.job_id,
            run_record,
            attempt_number=1,
            lease_token=LEASE_TOKEN_A,
        ).status
        is JobStatus.SUCCEEDED
    )

    stored_decision = ReleaseDecisionRecord(
        decision_id="decision-preexisting",
        decision=decision(data, baseline, candidate),
        created_at=NOW + timedelta(seconds=2),
    )
    repository.put_release_decision(stored_decision)
    decision_job = job(
        job_id="job-preexisting-decision",
        kind=JobKind.COMPARISON,
        idempotency_key="request-preexisting-decision",
        resource_id=stored_decision.decision_id,
    )
    payload = comparison_payload(data, baseline, candidate)
    activate_job(engine, repository, decision_job, payload)
    assert (
        repository.complete_release_decision(
            decision_job.job_id,
            stored_decision,
            attempt_number=1,
            lease_token=LEASE_TOKEN_A,
        ).status
        is JobStatus.SUCCEEDED
    )

    canceled_decision = ReleaseDecisionRecord(
        decision_id="decision-canceled-before-publication",
        decision=decision(data, baseline, candidate),
        created_at=NOW + timedelta(seconds=3),
    )
    canceled_job = job(
        job_id="job-canceled-decision",
        kind=JobKind.COMPARISON,
        idempotency_key="request-canceled-decision",
        resource_id=canceled_decision.decision_id,
    )
    activate_job(engine, repository, canceled_job, payload)
    repository.cancel_job(canceled_job.job_id)
    assert (
        repository.complete_release_decision(
            canceled_job.job_id,
            canceled_decision,
            attempt_number=1,
            lease_token=LEASE_TOKEN_A,
        ).status
        is JobStatus.CANCELED
    )
    with raises(RecordNotFoundError, match="not found"):
        repository.get_release_decision(canceled_decision.decision_id)


def test_retry_failure_and_cancellation_are_fenced_and_cancellation_wins(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    retry_record = job()
    activate_job(engine, repository, retry_record, run_payload())
    queued = repository.retry_job(
        retry_record.job_id,
        1,
        LEASE_TOKEN_A,
        delay_seconds=5,
        error_code="temporary_failure",
    )
    retry_attempt = repository.list_job_attempts(retry_record.job_id)[0]
    assert queued.status is JobStatus.QUEUED
    assert queued.available_at > queued.updated_at
    assert retry_attempt.status is JobAttemptStatus.RETRY_SCHEDULED
    assert retry_attempt.error_code == "temporary_failure"
    with raises(LeaseLostError, match="no longer active"):
        repository.fail_job(
            retry_record.job_id,
            1,
            LEASE_TOKEN_A,
            error_code="stale_worker",
        )

    failed_record = job(
        job_id="job-failed",
        idempotency_key="request-failed",
        resource_id="run-failed",
    )
    activate_job(engine, repository, failed_record, run_payload())
    failed = repository.fail_job(
        failed_record.job_id,
        1,
        LEASE_TOKEN_A,
        error_code="execution_failed",
    )
    assert failed.status is JobStatus.FAILED
    assert failed.error_code == "execution_failed"
    assert repository.list_job_attempts(failed.job_id)[0].status is (
        JobAttemptStatus.FAILED
    )

    canceled_record = job(
        job_id="job-cancel-running",
        idempotency_key="request-cancel-running",
        resource_id="run-cancel-running",
    )
    activate_job(engine, repository, canceled_record, run_payload())
    requested = repository.cancel_job(canceled_record.job_id)
    canceled = repository.retry_job(
        canceled_record.job_id,
        1,
        LEASE_TOKEN_A,
        delay_seconds=5,
        error_code="temporary_failure",
    )
    assert requested.status is JobStatus.CANCEL_REQUESTED
    assert canceled.status is JobStatus.CANCELED
    assert (
        repository.acknowledge_cancellation(
            canceled.job_id,
            1,
            LEASE_TOKEN_A,
        )
        == canceled
    )
    with raises(LeaseLostError, match="no longer active"):
        repository.acknowledge_cancellation(
            canceled.job_id,
            1,
            LEASE_TOKEN_B,
        )


def test_expired_or_stale_workers_never_mutate_or_publish(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    proposed = job(resource_id="run-expired")
    activate_job(
        engine,
        repository,
        proposed,
        run_payload(data),
        expired=True,
    )
    evidence = RunRecord(
        result=execute(data, run_id="run-expired"),
        created_at=NOW,
    )

    with raises(LeaseLostError, match="no longer active"):
        repository.complete_run(
            proposed.job_id,
            evidence,
            attempt_number=1,
            lease_token=LEASE_TOKEN_A,
        )
    with raises(LeaseLostError, match="no longer active"):
        repository.retry_job(
            proposed.job_id,
            1,
            LEASE_TOKEN_A,
            delay_seconds=5,
            error_code="temporary_failure",
        )

    assert repository.get_job(proposed.job_id).status is JobStatus.RUNNING
    with raises(RecordNotFoundError, match="not found"):
        repository.get_run(evidence.run_id)


def test_run_insert_retries_detect_conflicts_and_missing_dependencies(
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    first = RunRecord(result=execute(data, run_id="run-001"), created_at=NOW)

    assert repository.put_run(first) == first
    assert repository.put_run(first) == first

    different = RunRecord(
        result=execute(data, run_id="run-001", target_revision=2),
        created_at=NOW,
    )
    with raises(ImmutableRecordConflictError, match="different evidence"):
        repository.put_run(different)

    unregistered = dataset(name="unregistered")
    missing_dependency = RunRecord(
        result=execute(unregistered, run_id="run-unregistered"),
        created_at=NOW,
    )
    with raises(ImmutableRecordConflictError, match="metadata"):
        repository.put_run(missing_dependency)


def test_succeeded_replays_require_preserved_identical_evidence(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    first = RunRecord(result=execute(data, run_id="run-replay"), created_at=NOW)
    proposed = job(resource_id="run-replay")
    activate_job(engine, repository, proposed, run_payload(data))
    completed = repository.complete_run(
        proposed.job_id,
        first,
        attempt_number=1,
        lease_token=LEASE_TOKEN_A,
    )

    with engine.begin() as connection:
        connection.execute(
            delete(runs_table).where(runs_table.c.run_id == first.run_id)
        )
    with raises(CorruptRecordError, match="no stored evidence"):
        repository.complete_run(
            proposed.job_id,
            first,
            attempt_number=1,
            lease_token=LEASE_TOKEN_A,
        )

    different = RunRecord(
        result=execute(data, run_id="run-replay", target_revision=2),
        created_at=NOW,
    )
    repository.put_run(different)
    with raises(ImmutableRecordConflictError, match="different evidence"):
        repository.complete_run(
            proposed.job_id,
            first,
            attempt_number=1,
            lease_token=LEASE_TOKEN_A,
        )
    assert repository.get_job(proposed.job_id) == completed


def test_decision_completion_replays_require_identical_evidence(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    baseline = execute(data, run_id="baseline", target_revision=1)
    candidate = execute(data, run_id="candidate", target_revision=2)
    replacement = execute(data, run_id="replacement", target_revision=3)
    for index, result in enumerate((baseline, candidate, replacement)):
        repository.put_run(
            RunRecord(result=result, created_at=NOW + timedelta(seconds=index))
        )
    first = ReleaseDecisionRecord(
        decision_id="decision-replay",
        decision=decision(data, baseline, candidate),
        created_at=NOW + timedelta(seconds=3),
    )
    proposed = job(
        job_id="job-decision-replay",
        kind=JobKind.COMPARISON,
        idempotency_key="compare-replay",
        resource_id=first.decision_id,
    )

    with raises(LeaseLostError, match="no longer active"):
        repository.complete_release_decision(
            "missing-job",
            first,
            attempt_number=1,
            lease_token=LEASE_TOKEN_A,
        )

    activate_job(
        engine,
        repository,
        proposed,
        comparison_payload(data, baseline, candidate),
    )
    completed = repository.complete_release_decision(
        proposed.job_id,
        first,
        attempt_number=1,
        lease_token=LEASE_TOKEN_A,
    )
    assert (
        repository.complete_release_decision(
            proposed.job_id,
            first,
            attempt_number=1,
            lease_token=LEASE_TOKEN_A,
        )
        == completed
    )

    with engine.begin() as connection:
        connection.execute(
            delete(release_decisions_table).where(
                release_decisions_table.c.decision_id == first.decision_id
            )
        )
    with raises(CorruptRecordError, match="no stored evidence"):
        repository.complete_release_decision(
            proposed.job_id,
            first,
            attempt_number=1,
            lease_token=LEASE_TOKEN_A,
        )

    different = ReleaseDecisionRecord(
        decision_id=first.decision_id,
        decision=decision(data, baseline, replacement),
        created_at=first.created_at,
    )
    repository.put_release_decision(different)
    with raises(ImmutableRecordConflictError, match="different evidence"):
        repository.complete_release_decision(
            proposed.job_id,
            first,
            attempt_number=1,
            lease_token=LEASE_TOKEN_A,
        )


def test_fenced_completion_lets_cancellation_win_without_evidence(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    proposed = job(resource_id="run-canceled")
    activate_job(engine, repository, proposed, run_payload(data))
    requested = repository.cancel_job(proposed.job_id)
    evidence = RunRecord(
        result=execute(data, run_id="run-canceled"),
        created_at=NOW,
    )

    canceled = repository.complete_run(
        proposed.job_id,
        evidence,
        attempt_number=1,
        lease_token=LEASE_TOKEN_A,
    )

    assert requested.status is JobStatus.CANCEL_REQUESTED
    assert canceled.status is JobStatus.CANCELED
    assert repository.list_job_attempts(proposed.job_id)[0].status is (
        JobAttemptStatus.CANCELED
    )
    with raises(RecordNotFoundError, match="not found"):
        repository.get_run(evidence.run_id)


def test_completion_rejects_wrong_resource_and_stale_token_before_publish(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    proposed = job(resource_id="run-owned")
    activate_job(engine, repository, proposed, run_payload(data))
    wrong = RunRecord(
        result=execute(data, run_id="run-unowned"),
        created_at=NOW,
    )
    owned = RunRecord(
        result=execute(data, run_id="run-owned"),
        created_at=NOW,
    )

    with raises(IllegalJobTransitionError, match="does not own"):
        repository.complete_run(
            proposed.job_id,
            wrong,
            attempt_number=1,
            lease_token=LEASE_TOKEN_A,
        )
    with raises(LeaseLostError, match="no longer active"):
        repository.complete_run(
            proposed.job_id,
            owned,
            attempt_number=1,
            lease_token=LEASE_TOKEN_B,
        )

    with raises(RecordNotFoundError, match="not found"):
        repository.get_run(owned.run_id)


def test_each_list_rejects_a_well_formed_cursor_with_the_wrong_key_shape(
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    jobs_cursor = _encode_cursor(
        stream="jobs",
        filters={"kind": None, "status": None},
        key=[NOW.isoformat()],
    )
    runs_cursor = _encode_cursor(
        stream="runs",
        filters={"dataset_name": None},
        key=[NOW.isoformat()],
    )
    decisions_cursor = _encode_cursor(
        stream="release-decisions",
        filters={"status": None},
        key=[NOW.isoformat()],
    )
    datasets_cursor = _encode_cursor(
        stream="datasets",
        filters={"name": None},
        key=[NOW.isoformat(), "fixture", True],
    )

    with raises(InvalidCursorError, match="invalid"):
        repository.list_jobs(limit=1, cursor=jobs_cursor)
    with raises(InvalidCursorError, match="invalid"):
        repository.list_runs(limit=1, cursor=runs_cursor)
    with raises(InvalidCursorError, match="invalid"):
        repository.list_release_decisions(limit=1, cursor=decisions_cursor)
    with raises(InvalidCursorError, match="invalid"):
        repository.list_datasets(limit=1, cursor=datasets_cursor)
