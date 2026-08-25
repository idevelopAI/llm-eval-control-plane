from __future__ import annotations

import asyncio
import json
import os
import secrets
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from threading import Barrier
from threading import Event as ThreadEvent
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from opentelemetry.trace import Tracer
from pytest import MonkeyPatch
from sqlalchemy import create_engine, delete, event, insert, select, text, update
from sqlalchemy.engine import Engine, make_url

from llm_eval_control_plane.adapters.control_plane_db import (
    SqlAlchemyControlPlaneRepository,
    datasets_table,
    job_attempts_table,
    job_payloads_table,
    jobs_table,
    release_decisions_table,
    runs_table,
)
from llm_eval_control_plane.api import runtime
from llm_eval_control_plane.api.contracts import DatasetCreateRequest
from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.api.security import (
    AuthenticationConfiguration,
    ControlPlaneScope,
    PrincipalConfiguration,
    digest_token,
)
from llm_eval_control_plane.application.control_plane import (
    ClaimedJob,
    ControlPlaneRepository,
    StoreLeaseLostError,
    StoreTransitionError,
)
from llm_eval_control_plane.application.worker import (
    WorkerResult,
    WorkerResultStatus,
    WorkerService,
)
from llm_eval_control_plane.domain.canonical import sha256_digest
from llm_eval_control_plane.domain.control_plane import (
    DatasetRecord,
    ExecutionContract,
    JobAttemptStatus,
    JobKind,
    JobRecord,
    JobStatus,
    RunJobPayload,
    RunRecord,
)
from llm_eval_control_plane.domain.datasets import DatasetVersion
from llm_eval_control_plane.domain.results import RunResult

_DATASET_NAME = "phase5-integration/restart"
_KEY_PREFIX = "phase5-it-"
_API_KEY = f"{_KEY_PREFIX}api-restart"
_LEASE_SECONDS = 30
_OLD_TIME = datetime(2020, 1, 1, tzinfo=UTC)
_PROJECT_ID = "phase6-integration"


class CountingExecutor(DeterministicEvaluationExecutor):
    def __init__(self, *, tracer: Tracer | None = None) -> None:
        if tracer is None:
            super().__init__()
        else:
            super().__init__(tracer=tracer)
        self.validate_calls = 0
        self.execute_calls = 0

    def validate(
        self,
        *,
        target_name: str,
        target_revision: int,
        adapter: str,
        evaluator_names: tuple[str, ...],
        scenario_overrides: Mapping[str, str],
    ) -> ExecutionContract:
        self.validate_calls += 1
        return super().validate(
            target_name=target_name,
            target_revision=target_revision,
            adapter=adapter,
            evaluator_names=evaluator_names,
            scenario_overrides=scenario_overrides,
        )

    async def execute(
        self,
        *,
        run_id: str,
        dataset: DatasetVersion,
        target_name: str,
        target_revision: int,
        adapter: str,
        evaluator_names: tuple[str, ...],
        scenario_overrides: Mapping[str, str],
    ) -> RunResult:
        self.execute_calls += 1
        return await super().execute(
            run_id=run_id,
            dataset=dataset,
            target_name=target_name,
            target_revision=target_revision,
            adapter=adapter,
            evaluator_names=evaluator_names,
            scenario_overrides=scenario_overrides,
        )


def _repository(engine: Engine) -> ControlPlaneRepository:
    return cast(ControlPlaneRepository, SqlAlchemyControlPlaneRepository(engine))


def _test_job_ids() -> Any:
    return select(jobs_table.c.job_id).where(
        jobs_table.c.idempotency_key.like(f"{_KEY_PREFIX}%")
    )


def _test_run_ids() -> Any:
    return select(jobs_table.c.resource_id).where(
        jobs_table.c.idempotency_key.like(f"{_KEY_PREFIX}%"),
        jobs_table.c.kind == JobKind.RUN.value,
    )


def _test_decision_ids() -> Any:
    return select(jobs_table.c.resource_id).where(
        jobs_table.c.idempotency_key.like(f"{_KEY_PREFIX}%"),
        jobs_table.c.kind == JobKind.COMPARISON.value,
    )


def _clear_test_records(engine: Engine) -> None:
    job_ids = _test_job_ids()
    run_ids = _test_run_ids()
    decision_ids = _test_decision_ids()
    with engine.begin() as connection:
        connection.execute(
            delete(release_decisions_table).where(
                release_decisions_table.c.decision_id.in_(decision_ids)
            )
        )
        connection.execute(delete(runs_table).where(runs_table.c.run_id.in_(run_ids)))
        connection.execute(
            delete(job_attempts_table).where(job_attempts_table.c.job_id.in_(job_ids))
        )
        connection.execute(
            delete(job_payloads_table).where(job_payloads_table.c.job_id.in_(job_ids))
        )
        connection.execute(delete(jobs_table).where(jobs_table.c.job_id.in_(job_ids)))
        connection.execute(
            delete(datasets_table).where(datasets_table.c.name == _DATASET_NAME)
        )


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    raw_url = os.environ.get("CONTROL_PLANE_DATABASE_URL")
    if raw_url is None:
        pytest.skip("CONTROL_PLANE_DATABASE_URL is required for PostgreSQL integration")
    if make_url(raw_url).get_backend_name() != "postgresql":
        pytest.skip("real PostgreSQL is required for this integration test")

    engine = create_engine(raw_url, pool_pre_ping=True, hide_parameters=True)
    repository = _repository(engine)
    repository.check_health()
    assert repository.schema_is_current()
    _clear_test_records(engine)
    try:
        yield engine
    finally:
        _clear_test_records(engine)
        engine.dispose()


def _dataset_body() -> dict[str, object]:
    return {
        "name": _DATASET_NAME,
        "revision": 1,
        "cases": [
            {
                "case_id": "postgres-echo-001",
                "input": {
                    "scenario": "echo",
                    "value": "private-phase5-postgres-sentinel",
                },
                "expected": "private-phase5-postgres-sentinel",
                "slices": ["integration"],
            }
        ],
    }


def _run_body(*, target_name: str = "fake/phase5-postgres") -> dict[str, object]:
    return {
        "dataset_name": _DATASET_NAME,
        "dataset_revision": 1,
        "target_name": target_name,
        "target_revision": 1,
        "evaluators": ["exact_match"],
    }


def _json_document(response: Response) -> dict[str, Any]:
    document = response.json()
    assert isinstance(document, dict)
    return cast(dict[str, Any], document)


def _ensure_dataset(repository: ControlPlaneRepository) -> DatasetRecord:
    dataset = DatasetCreateRequest.model_validate(_dataset_body()).to_domain()
    return repository.put_dataset(
        DatasetRecord(dataset=dataset, created_at=datetime.now(UTC))
    )


def _key(suffix: str) -> str:
    return f"{_KEY_PREFIX}{suffix}"


def _token(label: str) -> str:
    return f"phase5_{label}_lease_token_{'x' * 40}"


def _runtime_authentication(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    token = "cpk_" + secrets.token_urlsafe(32)
    configuration = AuthenticationConfiguration(
        schema_version="control-plane-auth/v1",
        project_id=_PROJECT_ID,
        principals=(
            PrincipalConfiguration(
                principal_id="phase6-integration-operator",
                token_digest=digest_token(token),
                scopes=tuple(sorted(ControlPlaneScope, key=lambda item: item.value)),
            ),
        ),
    )
    document = configuration.model_dump_json()
    assert token not in document
    path = tmp_path / "control-plane-auth.json"
    path.write_text(document, encoding="utf-8")
    path.chmod(0o600)
    return path, {
        "Authorization": f"Bearer {token}",
        "X-Project-ID": _PROJECT_ID,
    }


def _enqueue_run(
    repository: ControlPlaneRepository,
    *,
    suffix: str,
    created_at: datetime = _OLD_TIME,
    max_attempts: int = 3,
) -> JobRecord:
    dataset = repository.get_dataset(_DATASET_NAME, 1)
    target_name = f"fake/phase5-{suffix}"
    executor = DeterministicEvaluationExecutor()
    evaluator_names = ("exact_match",)
    payload = RunJobPayload(
        dataset=dataset.dataset.artifact_ref,
        target_name=target_name,
        target_revision=1,
        adapter="deterministic_fake",
        evaluator_names=evaluator_names,
        execution_contract=executor.validate(
            target_name=target_name,
            target_revision=1,
            adapter="deterministic_fake",
            evaluator_names=evaluator_names,
            scenario_overrides={},
        ),
    )
    record = JobRecord(
        job_id=f"job_phase5_{suffix}",
        kind=JobKind.RUN,
        status=JobStatus.QUEUED,
        idempotency_key=_key(suffix),
        request_digest=sha256_digest({"phase5_integration": suffix}),
        resource_id=f"run_phase5_{suffix}",
        attempt_count=0,
        max_attempts=max_attempts,
        available_at=created_at,
        created_at=created_at,
        updated_at=created_at,
    )
    stored, created = repository.begin_job(record, payload)
    assert created
    return stored


def _expire_attempt(engine: Engine, claim: ClaimedJob) -> None:
    with engine.begin() as connection:
        changed = connection.execute(
            update(job_attempts_table)
            .where(
                job_attempts_table.c.job_id == claim.job.job_id,
                job_attempts_table.c.attempt_number == claim.attempt.attempt_number,
                job_attempts_table.c.status == JobAttemptStatus.RUNNING.value,
            )
            .values(
                started_at=text("statement_timestamp() - interval '3 seconds'"),
                heartbeat_at=text("statement_timestamp() - interval '2 seconds'"),
                lease_expires_at=text("statement_timestamp() - interval '1 second'"),
            )
        )
        assert changed.rowcount == 1


def _make_available_now(engine: Engine, job_id: str) -> None:
    with engine.begin() as connection:
        changed = connection.execute(
            update(jobs_table)
            .where(
                jobs_table.c.job_id == job_id,
                jobs_table.c.status == JobStatus.QUEUED.value,
            )
            .values(available_at=text("statement_timestamp()"))
        )
        assert changed.rowcount == 1


def _build_run_evidence(
    repository: ControlPlaneRepository,
    claim: ClaimedJob,
) -> RunRecord:
    payload = claim.payload
    assert isinstance(payload, RunJobPayload)
    dataset = repository.get_dataset(payload.dataset.name, payload.dataset.revision)
    executor = DeterministicEvaluationExecutor()
    result = asyncio.run(
        executor.execute(
            run_id=claim.job.resource_id,
            dataset=dataset.dataset,
            target_name=payload.target_name,
            target_revision=payload.target_revision,
            adapter=payload.adapter,
            evaluator_names=payload.evaluator_names,
            scenario_overrides={
                item.case_id: item.scenario for item in payload.scenario_overrides
            },
        )
    )
    return RunRecord(result=result, created_at=datetime.now(UTC))


def _run_count(engine: Engine, run_id: str) -> int:
    with engine.connect() as connection:
        return len(
            connection.execute(
                select(runs_table.c.run_id).where(runs_table.c.run_id == run_id)
            ).all()
        )


def test_api_enqueue_survives_restart_and_terminal_replay_is_redacted(
    postgres_engine: Engine,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "CONTROL_PLANE_DATABASE_URL",
        postgres_engine.url.render_as_string(hide_password=False),
    )
    auth_file, auth_headers = _runtime_authentication(tmp_path)
    monkeypatch.setenv("CONTROL_PLANE_AUTH_FILE", str(auth_file))
    api_executors: list[CountingExecutor] = []

    def create_executor(*, tracer: Tracer) -> CountingExecutor:
        executor = CountingExecutor(tracer=tracer)
        api_executors.append(executor)
        return executor

    monkeypatch.setattr(runtime, "DeterministicEvaluationExecutor", create_executor)
    body = _run_body()

    with TestClient(
        runtime.create_runtime_app(),
        raise_server_exceptions=False,
        headers=auth_headers,
    ) as first_client:
        dataset_response = first_client.post("/v1/datasets", json=_dataset_body())
        submitted = first_client.post(
            "/v1/runs",
            json=body,
            headers={"Idempotency-Key": _API_KEY},
        )
        assert dataset_response.status_code == 201
        assert submitted.status_code == 202
        submitted_document = _json_document(submitted)
        assert submitted_document["job"]["status"] == JobStatus.QUEUED.value
        assert submitted_document["run"] is None
        job_id = cast(str, submitted_document["job"]["job_id"])

    assert len(api_executors) == 1
    assert api_executors[0].validate_calls == 1
    assert api_executors[0].execute_calls == 0

    with TestClient(
        runtime.create_runtime_app(),
        raise_server_exceptions=False,
        headers=auth_headers,
    ) as restarted_client:
        loaded = restarted_client.get(f"/v1/jobs/{job_id}")
        replayed_queued = restarted_client.post(
            "/v1/runs",
            json=body,
            headers={"Idempotency-Key": _API_KEY},
        )
        assert loaded.status_code == 200
        assert _json_document(loaded)["status"] == JobStatus.QUEUED.value
        assert replayed_queued.status_code == 202
        assert _json_document(replayed_queued)["job"]["job_id"] == job_id

    assert len(api_executors) == 2
    assert api_executors[1].validate_calls == 0
    assert api_executors[1].execute_calls == 0

    worker_executor = CountingExecutor()
    worker = WorkerService(
        repository=_repository(postgres_engine),
        executor=worker_executor,
        worker_id="phase5-api-worker",
        lease_seconds=_LEASE_SECONDS,
        heartbeat_seconds=10,
        lease_token_factory=lambda: _token("api"),
    )
    worker_result = asyncio.run(worker.run_once())
    assert worker_result.status is WorkerResultStatus.SUCCEEDED
    assert worker_result.job_id == job_id
    assert worker_executor.execute_calls == 1

    with TestClient(
        runtime.create_runtime_app(),
        raise_server_exceptions=False,
        headers=auth_headers,
    ) as terminal_client:
        replayed_terminal = terminal_client.post(
            "/v1/runs",
            json=body,
            headers={"Idempotency-Key": _API_KEY},
        )
        attempts = terminal_client.get(f"/v1/jobs/{job_id}/attempts")
        assert replayed_terminal.status_code == 200
        terminal_document = _json_document(replayed_terminal)
        assert terminal_document["job"]["job_id"] == job_id
        assert terminal_document["job"]["status"] == JobStatus.SUCCEEDED.value
        assert terminal_document["run"] is not None
        assert attempts.status_code == 200
        attempt_document = _json_document(attempts)
        assert [item["status"] for item in attempt_document["items"]] == [
            JobAttemptStatus.SUCCEEDED.value
        ]

        public_json = json.dumps([terminal_document, attempt_document], sort_keys=True)
        for private_value in (
            "private-phase5-postgres-sentinel",
            _API_KEY,
            "phase5-api-worker",
            _token("api"),
            "idempotency_key",
            "request_digest",
            "lease_token",
            "worker_id",
            '"input"',
            '"output"',
            '"expected"',
        ):
            assert private_value not in public_json

    assert len(api_executors) == 3
    assert api_executors[2].validate_calls == 0
    assert api_executors[2].execute_calls == 0


def test_two_postgres_workers_claim_distinct_jobs(
    postgres_engine: Engine,
) -> None:
    primary = _repository(postgres_engine)
    _ensure_dataset(primary)
    jobs = (
        _enqueue_run(primary, suffix="distinct-a", created_at=_OLD_TIME),
        _enqueue_run(
            primary,
            suffix="distinct-b",
            created_at=_OLD_TIME + timedelta(seconds=1),
        ),
    )
    engine_a = create_engine(
        postgres_engine.url, pool_pre_ping=True, hide_parameters=True
    )
    engine_b = create_engine(
        postgres_engine.url, pool_pre_ping=True, hide_parameters=True
    )
    repository_a = _repository(engine_a)
    repository_b = _repository(engine_b)
    barrier = Barrier(2)

    def claim(repository: ControlPlaneRepository, label: str) -> ClaimedJob | None:
        barrier.wait(timeout=10)
        return repository.claim_next_job(
            worker_id=f"phase5-worker-{label}",
            lease_token=_token(label),
            lease_seconds=_LEASE_SECONDS,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(claim, repository_a, "a")
            second_future = pool.submit(claim, repository_b, "b")
            first = first_future.result(timeout=10)
            second = second_future.result(timeout=10)
    finally:
        engine_a.dispose()
        engine_b.dispose()

    assert first is not None
    assert second is not None
    assert {first.job.job_id, second.job.job_id} == {job.job_id for job in jobs}
    assert first.job.job_id != second.job.job_id
    assert first.attempt.attempt_number == second.attempt.attempt_number == 1


def test_skip_locked_and_future_availability_do_not_block_claiming(
    postgres_engine: Engine,
) -> None:
    repository = _repository(postgres_engine)
    _ensure_dataset(repository)
    oldest = _enqueue_run(repository, suffix="locked-oldest", created_at=_OLD_TIME)
    next_job = _enqueue_run(
        repository,
        suffix="locked-next",
        created_at=_OLD_TIME + timedelta(seconds=1),
    )
    future_job = _enqueue_run(
        repository,
        suffix="future",
        created_at=_OLD_TIME + timedelta(seconds=2),
    )
    with postgres_engine.begin() as connection:
        connection.execute(
            update(jobs_table)
            .where(jobs_table.c.job_id == future_job.job_id)
            .values(available_at=text("statement_timestamp() + interval '1 hour'"))
        )

    lock_connection = postgres_engine.connect()
    lock_transaction = lock_connection.begin()
    lock_connection.execute(
        select(jobs_table.c.job_id)
        .where(jobs_table.c.job_id == oldest.job_id)
        .with_for_update()
    ).one()
    claim_engine = create_engine(
        postgres_engine.url, pool_pre_ping=True, hide_parameters=True
    )
    claim_repository = _repository(claim_engine)
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(
        claim_repository.claim_next_job,
        worker_id="phase5-skip-locked-worker",
        lease_token=_token("skip_locked"),
        lease_seconds=_LEASE_SECONDS,
    )
    blocked = False
    try:
        try:
            claim = future.result(timeout=3)
        except FutureTimeoutError:
            blocked = True
            claim = None
    finally:
        lock_transaction.rollback()
        lock_connection.close()
        if blocked:
            future.result(timeout=10)
        pool.shutdown(wait=True, cancel_futures=True)
        claim_engine.dispose()

    assert not blocked, "claim blocked on a row lock instead of using SKIP LOCKED"
    assert claim is not None
    assert claim.job.job_id == next_job.job_id

    remaining = repository.claim_next_job(
        worker_id="phase5-after-lock-worker",
        lease_token=_token("after_lock"),
        lease_seconds=_LEASE_SECONDS,
    )
    assert remaining is not None
    assert remaining.job.job_id == oldest.job_id
    assert (
        repository.claim_next_job(
            worker_id="phase5-future-worker",
            lease_token=_token("future"),
            lease_seconds=_LEASE_SECONDS,
        )
        is None
    )


def test_wrong_expired_and_stale_heartbeat_tokens_are_fenced(
    postgres_engine: Engine,
) -> None:
    repository = _repository(postgres_engine)
    _ensure_dataset(repository)
    job = _enqueue_run(repository, suffix="heartbeat", max_attempts=2)
    old_token = _token("heartbeat_old")
    claim = repository.claim_next_job(
        worker_id="phase5-heartbeat-worker",
        lease_token=old_token,
        lease_seconds=_LEASE_SECONDS,
    )
    assert claim is not None

    with pytest.raises(StoreLeaseLostError):
        repository.heartbeat_job(
            job.job_id,
            claim.attempt.attempt_number,
            _token("heartbeat_wrong"),
            lease_seconds=_LEASE_SECONDS,
        )

    _expire_attempt(postgres_engine, claim)
    with pytest.raises(StoreLeaseLostError):
        repository.heartbeat_job(
            job.job_id,
            claim.attempt.attempt_number,
            old_token,
            lease_seconds=_LEASE_SECONDS,
        )

    reaped = repository.reap_expired_jobs(
        limit=10,
        retry_base_seconds=1,
        retry_max_seconds=1,
    )
    assert [record.job_id for record in reaped] == [job.job_id]
    assert reaped[0].status is JobStatus.QUEUED
    _make_available_now(postgres_engine, job.job_id)
    new_token = _token("heartbeat_new")
    reclaimed = repository.claim_next_job(
        worker_id="phase5-heartbeat-worker-new",
        lease_token=new_token,
        lease_seconds=_LEASE_SECONDS,
    )
    assert reclaimed is not None
    assert reclaimed.attempt.attempt_number == 2

    with pytest.raises(StoreLeaseLostError):
        repository.heartbeat_job(
            job.job_id,
            claim.attempt.attempt_number,
            old_token,
            lease_seconds=_LEASE_SECONDS,
        )
    heartbeated = repository.heartbeat_job(
        job.job_id,
        reclaimed.attempt.attempt_number,
        new_token,
        lease_seconds=_LEASE_SECONDS,
    )
    assert heartbeated.status is JobStatus.RUNNING


@pytest.mark.parametrize("operation", ("heartbeat", "completion"))
def test_lock_wait_cannot_authorize_work_after_lease_expiry(
    postgres_engine: Engine,
    operation: str,
) -> None:
    primary = _repository(postgres_engine)
    _ensure_dataset(primary)
    job = _enqueue_run(primary, suffix=f"lock-expiry-{operation}")
    claim = primary.claim_next_job(
        worker_id=f"phase5-lock-expiry-{operation}",
        lease_token=_token(f"lock_expiry_{operation}"),
        lease_seconds=_LEASE_SECONDS,
    )
    assert claim is not None
    evidence = _build_run_evidence(primary, claim)

    call_engine = create_engine(
        postgres_engine.url,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    call_repository = _repository(call_engine)
    call_repository.check_health()
    lock_attempted = ThreadEvent()

    def observe_job_lock(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        normalized = " ".join(statement.upper().split())
        if (
            normalized.startswith("SELECT")
            and "FROM CONTROL_PLANE_JOBS" in normalized
            and "FOR UPDATE" in normalized
        ):
            lock_attempted.set()

    event.listen(call_engine, "before_cursor_execute", observe_job_lock)

    lock_connection = postgres_engine.connect()
    lock_transaction = lock_connection.begin()
    lock_connection.execute(
        select(jobs_table.c.job_id)
        .where(jobs_table.c.job_id == job.job_id)
        .with_for_update()
    ).one()
    changed = lock_connection.execute(
        update(job_attempts_table)
        .where(
            job_attempts_table.c.job_id == job.job_id,
            job_attempts_table.c.attempt_number == claim.attempt.attempt_number,
        )
        .values(
            started_at=text("statement_timestamp() - interval '2 seconds'"),
            heartbeat_at=text("statement_timestamp() - interval '1 second'"),
            lease_expires_at=text("statement_timestamp() + interval '2 seconds'"),
        )
    )
    assert changed.rowcount == 1

    def invoke() -> JobRecord:
        if operation == "heartbeat":
            return call_repository.heartbeat_job(
                job.job_id,
                claim.attempt.attempt_number,
                claim.lease_token,
                lease_seconds=_LEASE_SECONDS,
            )
        return call_repository.complete_run(
            job.job_id,
            evidence,
            attempt_number=claim.attempt.attempt_number,
            lease_token=claim.lease_token,
        )

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(invoke)
    try:
        assert lock_attempted.wait(timeout=5), "worker did not reach the row lock"
        lock_connection.execute(text("SELECT pg_sleep(3)"))
        lock_transaction.commit()
        with pytest.raises(StoreLeaseLostError):
            future.result(timeout=10)
    finally:
        if lock_transaction.is_active:
            lock_transaction.rollback()
        event.remove(call_engine, "before_cursor_execute", observe_job_lock)
        pool.shutdown(wait=True, cancel_futures=True)
        lock_connection.close()
        call_engine.dispose()

    assert primary.get_job(job.job_id).status is JobStatus.RUNNING
    assert _run_count(postgres_engine, job.resource_id) == 0


def test_completion_expiring_during_evidence_insert_rolls_back_publication(
    postgres_engine: Engine,
) -> None:
    primary = _repository(postgres_engine)
    _ensure_dataset(primary)
    job = _enqueue_run(primary, suffix="finish-expiry")
    claim = primary.claim_next_job(
        worker_id="phase5-finish-expiry",
        lease_token=_token("finish_expiry"),
        lease_seconds=_LEASE_SECONDS,
    )
    assert claim is not None
    evidence = _build_run_evidence(primary, claim)
    completion_engine = create_engine(
        postgres_engine.url,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    completion_repository = _repository(completion_engine)
    completion_repository.check_health()
    with postgres_engine.begin() as connection:
        changed = connection.execute(
            update(job_attempts_table)
            .where(
                job_attempts_table.c.job_id == job.job_id,
                job_attempts_table.c.attempt_number == claim.attempt.attempt_number,
            )
            .values(
                started_at=text("statement_timestamp() - interval '2 seconds'"),
                heartbeat_at=text("statement_timestamp() - interval '1 second'"),
                lease_expires_at=text("statement_timestamp() + interval '2 seconds'"),
            )
        )
        assert changed.rowcount == 1

    delayed_insert = False

    def delay_evidence_insert(
        _connection: object,
        cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        nonlocal delayed_insert
        if statement.lstrip().upper().startswith("INSERT INTO CONTROL_PLANE_RUNS"):
            delayed_insert = True
            cursor.execute("SELECT pg_sleep(3)")  # type: ignore[attr-defined]

    event.listen(completion_engine, "before_cursor_execute", delay_evidence_insert)
    try:
        with pytest.raises(StoreLeaseLostError):
            completion_repository.complete_run(
                job.job_id,
                evidence,
                attempt_number=claim.attempt.attempt_number,
                lease_token=claim.lease_token,
            )
    finally:
        event.remove(
            completion_engine,
            "before_cursor_execute",
            delay_evidence_insert,
        )
        completion_engine.dispose()

    assert delayed_insert, "completion expired before reaching evidence publication"
    assert primary.get_job(job.job_id).status is JobStatus.RUNNING
    assert primary.list_job_attempts(job.job_id)[0].status is JobAttemptStatus.RUNNING
    assert _run_count(postgres_engine, job.resource_id) == 0


def test_attempt_listing_stays_consistent_when_claim_commits_after_snapshot(
    postgres_engine: Engine,
) -> None:
    primary = _repository(postgres_engine)
    _ensure_dataset(primary)
    job = _enqueue_run(primary, suffix="attempt-snapshot")
    writer_connection = postgres_engine.connect()
    writer_transaction = writer_connection.begin()
    changed = writer_connection.execute(
        update(jobs_table)
        .where(jobs_table.c.job_id == job.job_id)
        .values(
            status=JobStatus.RUNNING.value,
            attempt_count=1,
            updated_at=text("statement_timestamp()"),
            version=jobs_table.c.version + 1,
        )
    )
    assert changed.rowcount == 1
    writer_connection.execute(
        insert(job_attempts_table).values(
            job_id=job.job_id,
            attempt_number=1,
            status=JobAttemptStatus.RUNNING.value,
            worker_id="phase5-attempt-snapshot",
            lease_token=_token("attempt_snapshot"),
            error_code=None,
            started_at=text("statement_timestamp()"),
            heartbeat_at=text("statement_timestamp()"),
            lease_expires_at=text("statement_timestamp() + interval '30 seconds'"),
            finished_at=None,
        )
    )

    reader_engine = create_engine(
        postgres_engine.url,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    reader = _repository(reader_engine)
    committed = False

    def commit_claim_after_snapshot(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        nonlocal committed
        normalized = " ".join(statement.upper().split())
        if not committed and "FROM CONTROL_PLANE_JOBS" in normalized:
            writer_transaction.commit()
            committed = True

    event.listen(reader_engine, "after_cursor_execute", commit_claim_after_snapshot)
    try:
        assert reader.list_job_attempts(job.job_id) == ()
    finally:
        if writer_transaction.is_active:
            writer_transaction.rollback()
        event.remove(
            reader_engine,
            "after_cursor_execute",
            commit_claim_after_snapshot,
        )
        writer_connection.close()
        reader_engine.dispose()

    assert committed
    assert len(primary.list_job_attempts(job.job_id)) == 1


def test_reaper_requeues_then_exhausts_attempts(
    postgres_engine: Engine,
) -> None:
    repository = _repository(postgres_engine)
    _ensure_dataset(repository)
    job = _enqueue_run(repository, suffix="reaper", max_attempts=2)
    first = repository.claim_next_job(
        worker_id="phase5-reaper-a",
        lease_token=_token("reaper_a"),
        lease_seconds=_LEASE_SECONDS,
    )
    assert first is not None
    _expire_attempt(postgres_engine, first)

    first_reap = repository.reap_expired_jobs(
        limit=1,
        retry_base_seconds=2,
        retry_max_seconds=2,
    )
    assert len(first_reap) == 1
    assert first_reap[0].status is JobStatus.QUEUED
    assert first_reap[0].attempt_count == 1
    assert first_reap[0].available_at > first_reap[0].updated_at

    _make_available_now(postgres_engine, job.job_id)
    second = repository.claim_next_job(
        worker_id="phase5-reaper-b",
        lease_token=_token("reaper_b"),
        lease_seconds=_LEASE_SECONDS,
    )
    assert second is not None
    assert second.attempt.attempt_number == 2
    _expire_attempt(postgres_engine, second)

    second_reap = repository.reap_expired_jobs(
        limit=1,
        retry_base_seconds=2,
        retry_max_seconds=2,
    )
    assert len(second_reap) == 1
    assert second_reap[0].status is JobStatus.FAILED
    assert second_reap[0].attempt_count == 2
    assert second_reap[0].error_code == "lease_expired"
    attempts = repository.list_job_attempts(job.job_id)
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]
    assert all(attempt.status is JobAttemptStatus.LEASE_EXPIRED for attempt in attempts)
    assert all(attempt.error_code == "lease_expired" for attempt in attempts)
    assert all(attempt.finished_at is not None for attempt in attempts)


def test_reclaimed_job_rejects_old_worker_publication(
    postgres_engine: Engine,
) -> None:
    repository = _repository(postgres_engine)
    _ensure_dataset(repository)
    job = _enqueue_run(repository, suffix="stale-publish", max_attempts=2)
    old_claim = repository.claim_next_job(
        worker_id="phase5-stale-worker",
        lease_token=_token("stale_old"),
        lease_seconds=_LEASE_SECONDS,
    )
    assert old_claim is not None
    _expire_attempt(postgres_engine, old_claim)
    repository.reap_expired_jobs(
        limit=1,
        retry_base_seconds=1,
        retry_max_seconds=1,
    )
    _make_available_now(postgres_engine, job.job_id)
    new_claim = repository.claim_next_job(
        worker_id="phase5-current-worker",
        lease_token=_token("stale_new"),
        lease_seconds=_LEASE_SECONDS,
    )
    assert new_claim is not None
    evidence = _build_run_evidence(repository, new_claim)

    with pytest.raises(StoreLeaseLostError):
        repository.complete_run(
            job.job_id,
            evidence,
            attempt_number=old_claim.attempt.attempt_number,
            lease_token=old_claim.lease_token,
        )
    assert _run_count(postgres_engine, job.resource_id) == 0

    completed = repository.complete_run(
        job.job_id,
        evidence,
        attempt_number=new_claim.attempt.attempt_number,
        lease_token=new_claim.lease_token,
    )
    assert completed.status is JobStatus.SUCCEEDED
    assert _run_count(postgres_engine, job.resource_id) == 1


def test_cancellation_and_completion_race_has_one_terminal_outcome(
    postgres_engine: Engine,
) -> None:
    primary = _repository(postgres_engine)
    _ensure_dataset(primary)
    job = _enqueue_run(primary, suffix="cancel-race")
    claim = primary.claim_next_job(
        worker_id="phase5-cancel-race-worker",
        lease_token=_token("cancel_race"),
        lease_seconds=_LEASE_SECONDS,
    )
    assert claim is not None
    evidence = _build_run_evidence(primary, claim)
    cancel_engine = create_engine(
        postgres_engine.url, pool_pre_ping=True, hide_parameters=True
    )
    complete_engine = create_engine(
        postgres_engine.url, pool_pre_ping=True, hide_parameters=True
    )
    cancel_repository = _repository(cancel_engine)
    complete_repository = _repository(complete_engine)
    barrier = Barrier(2)

    def cancel() -> JobRecord | StoreTransitionError:
        barrier.wait(timeout=10)
        try:
            return cancel_repository.cancel_job(job.job_id)
        except StoreTransitionError as error:
            return error

    def complete() -> JobRecord:
        barrier.wait(timeout=10)
        return complete_repository.complete_run(
            job.job_id,
            evidence,
            attempt_number=claim.attempt.attempt_number,
            lease_token=claim.lease_token,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            canceled_future = pool.submit(cancel)
            completed_future = pool.submit(complete)
            canceled_result = canceled_future.result(timeout=10)
            completed_result = completed_future.result(timeout=10)
    finally:
        cancel_engine.dispose()
        complete_engine.dispose()

    terminal = primary.get_job(job.job_id)
    assert terminal.status in {JobStatus.SUCCEEDED, JobStatus.CANCELED}
    assert completed_result.status is terminal.status
    if isinstance(canceled_result, StoreTransitionError):
        assert terminal.status is JobStatus.SUCCEEDED
    else:
        assert canceled_result.status in {
            JobStatus.CANCEL_REQUESTED,
            JobStatus.CANCELED,
        }
    evidence_count = _run_count(postgres_engine, job.resource_id)
    assert evidence_count == (1 if terminal.status is JobStatus.SUCCEEDED else 0)
    attempts = primary.list_job_attempts(job.job_id)
    assert len(attempts) == 1
    expected_attempt_status = (
        JobAttemptStatus.SUCCEEDED
        if terminal.status is JobStatus.SUCCEEDED
        else JobAttemptStatus.CANCELED
    )
    assert attempts[0].status is expected_attempt_status


def test_multi_worker_batch_publishes_every_job_once(
    postgres_engine: Engine,
) -> None:
    primary = _repository(postgres_engine)
    _ensure_dataset(primary)
    jobs = tuple(
        _enqueue_run(
            primary,
            suffix=f"batch-{index}",
            created_at=_OLD_TIME + timedelta(seconds=index),
        )
        for index in range(9)
    )
    engines = tuple(
        create_engine(postgres_engine.url, pool_pre_ping=True, hide_parameters=True)
        for _ in range(3)
    )

    def drain(worker_index: int) -> tuple[list[WorkerResult], int]:
        executor = CountingExecutor()
        token_numbers = count(1)
        worker = WorkerService(
            repository=_repository(engines[worker_index]),
            executor=executor,
            worker_id=f"phase5-batch-worker-{worker_index}",
            lease_seconds=_LEASE_SECONDS,
            heartbeat_seconds=10,
            lease_token_factory=lambda: _token(
                f"batch_{worker_index}_{next(token_numbers)}"
            ),
        )
        results: list[WorkerResult] = []
        while True:
            result = asyncio.run(worker.run_once())
            if result.status is WorkerResultStatus.IDLE:
                return results, executor.execute_calls
            results.append(result)

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = tuple(pool.submit(drain, index) for index in range(3))
            outcomes = tuple(future.result(timeout=30) for future in futures)
    finally:
        for engine in engines:
            engine.dispose()

    worker_results = [result for results, _calls in outcomes for result in results]
    assert len(worker_results) == len(jobs)
    assert all(
        result.status is WorkerResultStatus.SUCCEEDED for result in worker_results
    )
    assert {result.job_id for result in worker_results} == {job.job_id for job in jobs}
    assert sum(calls for _results, calls in outcomes) == len(jobs)

    with postgres_engine.connect() as connection:
        stored_run_ids = set(
            connection.execute(
                select(runs_table.c.run_id).where(
                    runs_table.c.run_id.in_(tuple(job.resource_id for job in jobs))
                )
            ).scalars()
        )
    assert stored_run_ids == {job.resource_id for job in jobs}
    for job in jobs:
        stored = primary.get_job(job.job_id)
        attempts = primary.list_job_attempts(job.job_id)
        assert stored.status is JobStatus.SUCCEEDED
        assert stored.attempt_count == 1
        assert len(attempts) == 1
        assert attempts[0].status is JobAttemptStatus.SUCCEEDED
