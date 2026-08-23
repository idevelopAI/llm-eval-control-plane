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
from sqlalchemy import create_engine, delete, event, inspect, select, text, update
from sqlalchemy.engine import Engine, RowMapping

from llm_eval_control_plane.adapters.control_plane_db import (
    CONTROL_PLANE_METADATA,
    ConcurrentTransitionError,
    ControlPlaneRepositoryError,
    CorruptRecordError,
    IdempotencyConflictError,
    IllegalJobTransitionError,
    ImmutableRecordConflictError,
    InvalidCursorError,
    PayloadTooLargeError,
    RecordNotFoundError,
    ResourceAlreadySubmittedError,
    SqlAlchemyControlPlaneRepository,
    _aware,
    _encode_cursor,
    datasets_table,
    jobs_table,
    release_decisions_table,
    runs_table,
)
from llm_eval_control_plane.adapters.fake_target import DeterministicFakeTarget
from llm_eval_control_plane.adapters.scorers import (
    BuiltInEvaluatorKind,
    build_evaluators,
)
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
    DatasetRecord,
    JobKind,
    JobRecord,
    JobStatus,
    ReleaseDecisionRecord,
    RunRecord,
)
from llm_eval_control_plane.domain.results import RunResult

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


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


def job(
    *,
    job_id: str = "job-001",
    kind: JobKind = JobKind.RUN,
    idempotency_key: str = "request-001",
    resource_id: str = "run-001",
    request: object = "same",
    created_at: datetime = NOW,
) -> JobRecord:
    return JobRecord(
        job_id=job_id,
        kind=kind,
        status=JobStatus.QUEUED,
        idempotency_key=idempotency_key,
        request_digest=sha256_digest(request),
        resource_id=resource_id,
        created_at=created_at,
        updated_at=created_at,
    )


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
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    proposed = job()
    stored, created = repository.begin_job(proposed)
    retry, retry_created = repository.begin_job(
        job(job_id="job-retry", resource_id="run-retry")
    )

    assert (stored, created) == (proposed, True)
    assert retry == proposed
    assert retry_created is False

    with raises(IdempotencyConflictError, match="different request"):
        repository.begin_job(
            job(job_id="job-other", resource_id="run-other", request="changed")
        )
    with raises(ResourceAlreadySubmittedError, match="already submitted"):
        repository.begin_job(
            job(
                job_id="job-resource",
                idempotency_key="request-resource",
                resource_id="run-001",
            )
        )


def test_concurrent_begin_job_has_exactly_one_insert_winner(engine: Engine) -> None:
    barrier = Barrier(2)

    def submit(record: JobRecord) -> tuple[JobRecord, bool]:
        barrier.wait()
        return SqlAlchemyControlPlaneRepository(engine).begin_job(record)

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


def test_job_transitions_are_atomic_legal_and_filterable(
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    proposed = job()
    repository.begin_job(proposed)

    with raises(IllegalJobTransitionError, match="not allowed"):
        repository.transition_job(
            proposed.job_id,
            JobStatus.FAILED,
            at=NOW + timedelta(seconds=1),
            error_code="execution_failed",
        )

    running = repository.transition_job(
        proposed.job_id, JobStatus.RUNNING, at=NOW + timedelta(seconds=1)
    )
    failed = repository.transition_job(
        proposed.job_id,
        JobStatus.FAILED,
        at=NOW + timedelta(seconds=2),
        error_code="execution_failed",
    )
    assert running.status is JobStatus.RUNNING
    assert failed.status is JobStatus.FAILED
    assert repository.list_jobs(limit=10, status=JobStatus.FAILED).items == (failed,)


def test_complete_run_is_atomic_idempotent_and_result_digest_is_not_unique(
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    first_result = execute(data, run_id="run-001")
    record = RunRecord(result=first_result, created_at=NOW + timedelta(seconds=2))
    proposed = job()
    repository.begin_job(proposed)
    repository.transition_job(
        proposed.job_id, JobStatus.RUNNING, at=NOW + timedelta(seconds=1)
    )

    completed = repository.complete_run(
        proposed.job_id, record, at=NOW + timedelta(seconds=3)
    )
    retried = repository.complete_run(
        proposed.job_id, record, at=NOW + timedelta(seconds=4)
    )
    assert completed.status is JobStatus.SUCCEEDED
    assert retried == completed
    assert repository.get_run("run-001") == record

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
    repository.begin_job(proposed)
    repository.transition_job(
        proposed.job_id, JobStatus.RUNNING, at=NOW + timedelta(seconds=1)
    )
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
                proposed.job_id, record, at=NOW + timedelta(seconds=3)
            )
    finally:
        event.remove(engine, "before_cursor_execute", fail_job_update)

    assert repository.get_job(proposed.job_id).status is JobStatus.RUNNING
    with raises(RecordNotFoundError, match="not found"):
        repository.get_run(record.run_id)


def test_release_decision_completion_is_append_only(
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
    repository.begin_job(proposed)
    repository.transition_job(
        proposed.job_id, JobStatus.RUNNING, at=NOW + timedelta(seconds=2)
    )

    completed = repository.complete_release_decision(
        proposed.job_id, evidence, at=NOW + timedelta(seconds=4)
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
        repository.begin_job(proposed)
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
        ("submit job", lambda: empty.begin_job(job())),
        ("load job", lambda: empty.get_job("private-job")),
        ("list jobs", lambda: empty.list_jobs(limit=1)),
        (
            "transition job",
            lambda: empty.transition_job(
                "private-job",
                JobStatus.RUNNING,
                at=NOW,
            ),
        ),
        (
            "complete run job",
            lambda: empty.complete_run("private-job", run_record, at=NOW),
        ),
        (
            "complete comparison job",
            lambda: empty.complete_release_decision(
                "private-job",
                decision_record,
                at=NOW,
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
    repository.begin_job(proposed)
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
    with raises(ValueError, match="queued state"):
        repository.begin_job(running)

    for limit in (True, 101):
        with raises(ValueError, match="between 1 and 100"):
            repository.list_jobs(limit=limit)


def test_transition_retries_and_compare_and_set_failures_are_safe(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    with raises(RecordNotFoundError, match="not found"):
        repository.transition_job("missing-job", JobStatus.RUNNING, at=NOW)

    proposed = job()
    repository.begin_job(proposed)
    running = repository.transition_job(
        proposed.job_id,
        JobStatus.RUNNING,
        at=NOW + timedelta(seconds=1),
    )
    assert (
        repository.transition_job(
            proposed.job_id,
            JobStatus.RUNNING,
            at=NOW + timedelta(seconds=2),
        )
        == running
    )

    contender = job(
        job_id="job-contender",
        idempotency_key="request-contender",
        resource_id="run-contender",
    )
    repository.begin_job(contender)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER reject_job_transition "
                "BEFORE UPDATE ON control_plane_jobs "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        )
    with raises(ConcurrentTransitionError, match="changed"):
        repository.transition_job(
            contender.job_id,
            JobStatus.RUNNING,
            at=NOW + timedelta(seconds=1),
        )
    assert repository.get_job(contender.job_id).status is JobStatus.QUEUED


def test_run_completion_enforces_job_identity_state_and_time(
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    evidence = RunRecord(
        result=execute(data, run_id="run-owned"),
        created_at=NOW + timedelta(seconds=2),
    )

    with raises(RecordNotFoundError, match="not found"):
        repository.complete_run("missing-job", evidence, at=NOW)

    proposed = job(resource_id="run-owned")
    repository.begin_job(proposed)
    with raises(IllegalJobTransitionError, match="running jobs"):
        repository.complete_run(proposed.job_id, evidence, at=NOW)

    repository.transition_job(
        proposed.job_id,
        JobStatus.RUNNING,
        at=NOW + timedelta(seconds=1),
    )
    unowned = RunRecord(
        result=execute(data, run_id="run-unowned"),
        created_at=NOW + timedelta(seconds=2),
    )
    with raises(IllegalJobTransitionError, match="does not own"):
        repository.complete_run(
            proposed.job_id,
            unowned,
            at=NOW + timedelta(seconds=2),
        )
    with raises(IllegalJobTransitionError, match="not allowed"):
        repository.complete_run(proposed.job_id, evidence, at=NOW)


def test_compare_and_set_completion_failure_rolls_back_evidence(
    engine: Engine,
    repository: SqlAlchemyControlPlaneRepository,
) -> None:
    data = dataset()
    repository.put_dataset(DatasetRecord(dataset=data, created_at=NOW))
    proposed = job(resource_id="run-cas")
    repository.begin_job(proposed)
    repository.transition_job(
        proposed.job_id,
        JobStatus.RUNNING,
        at=NOW + timedelta(seconds=1),
    )
    evidence = RunRecord(
        result=execute(data, run_id="run-cas"),
        created_at=NOW + timedelta(seconds=2),
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER reject_job_completion "
                "BEFORE UPDATE ON control_plane_jobs "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        )

    with raises(ConcurrentTransitionError, match="changed"):
        repository.complete_run(
            proposed.job_id,
            evidence,
            at=NOW + timedelta(seconds=3),
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
    repository.begin_job(proposed)
    repository.transition_job(
        proposed.job_id,
        JobStatus.RUNNING,
        at=NOW + timedelta(seconds=1),
    )
    completed = repository.complete_run(
        proposed.job_id,
        first,
        at=NOW + timedelta(seconds=2),
    )

    with engine.begin() as connection:
        connection.execute(
            delete(runs_table).where(runs_table.c.run_id == first.run_id)
        )
    with raises(CorruptRecordError, match="no stored evidence"):
        repository.complete_run(
            proposed.job_id,
            first,
            at=NOW + timedelta(seconds=3),
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
            at=NOW + timedelta(seconds=3),
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

    with raises(RecordNotFoundError, match="not found"):
        repository.complete_release_decision("missing-job", first, at=NOW)

    repository.begin_job(proposed)
    repository.transition_job(
        proposed.job_id,
        JobStatus.RUNNING,
        at=NOW + timedelta(seconds=1),
    )
    completed = repository.complete_release_decision(
        proposed.job_id,
        first,
        at=NOW + timedelta(seconds=4),
    )
    assert (
        repository.complete_release_decision(
            proposed.job_id,
            first,
            at=NOW + timedelta(seconds=5),
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
            at=NOW + timedelta(seconds=6),
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
            at=NOW + timedelta(seconds=6),
        )


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
