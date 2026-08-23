from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from pytest import MonkeyPatch, fixture, raises
from sqlalchemy import create_engine, insert, inspect, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from llm_eval_control_plane.adapters.control_plane_db import (
    CONTROL_PLANE_METADATA,
    job_attempts_table,
    job_payloads_table,
    jobs_table,
)
from llm_eval_control_plane.domain.canonical import sha256_digest

_REVISION_ONE = "20260820_0001"
_HEAD = "20260823_0002"
_CREATED_AT = datetime(2020, 1, 2, 3, tzinfo=UTC)
_FUTURE_CREATED_AT = datetime(2099, 1, 2, 3, tzinfo=UTC)
_FUTURE_UPDATED_AT = _FUTURE_CREATED_AT + timedelta(minutes=1)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@fixture
def database_url(tmp_path: Path, monkeypatch: MonkeyPatch) -> Iterator[str]:
    url = f"sqlite+pysqlite:///{tmp_path / 'migration.sqlite3'}"
    monkeypatch.setenv("CONTROL_PLANE_DATABASE_URL", url)
    yield url


def _insert_legacy_jobs(engine: Engine) -> None:
    statement = text(
        "INSERT INTO control_plane_jobs "
        "(job_id, kind, status, idempotency_key, request_digest, resource_id, "
        "error_code, created_at, updated_at, version) "
        "VALUES (:job_id, 'run', :status, :idempotency_key, :request_digest, "
        ":resource_id, :error_code, :created_at, :updated_at, 0)"
    )
    with engine.begin() as connection:
        for position, status in enumerate(
            ("queued", "running", "succeeded", "failed"),
            start=1,
        ):
            connection.execute(
                statement,
                {
                    "job_id": f"legacy-job-{status}",
                    "status": status,
                    "idempotency_key": f"legacy-key-{status}",
                    "request_digest": sha256_digest({"status": status}),
                    "resource_id": f"legacy-run-{status}",
                    "error_code": "execution_failed" if status == "failed" else None,
                    "created_at": _CREATED_AT,
                    "updated_at": _CREATED_AT + timedelta(seconds=position),
                },
            )
        connection.execute(
            statement,
            {
                "job_id": "legacy-job-future",
                "status": "running",
                "idempotency_key": "legacy-key-future",
                "request_digest": sha256_digest({"status": "future"}),
                "resource_id": "legacy-run-future",
                "error_code": None,
                "created_at": _FUTURE_CREATED_AT,
                "updated_at": _FUTURE_UPDATED_AT,
            },
        )


def test_upgrade_terminalizes_unrecoverable_legacy_jobs_and_records_history(
    database_url: str,
) -> None:
    config = Config("alembic.ini")
    command.upgrade(config, _REVISION_ONE)
    legacy_engine = create_engine(database_url)
    _insert_legacy_jobs(legacy_engine)
    legacy_engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            jobs = {
                row["job_id"]: row
                for row in connection.execute(select(jobs_table)).mappings()
            }
            attempts = {
                row["job_id"]: row
                for row in connection.execute(select(job_attempts_table)).mappings()
            }
            assert not connection.execute(select(job_payloads_table)).first()

            migration_context = MigrationContext.configure(connection)
            assert compare_metadata(migration_context, CONTROL_PLANE_METADATA) == []

        for status in ("queued", "running"):
            row = jobs[f"legacy-job-{status}"]
            assert row["status"] == "failed"
            assert row["error_code"] == "legacy_payload_missing"
            assert row["version"] == 1

        assert jobs["legacy-job-succeeded"]["status"] == "succeeded"
        assert jobs["legacy-job-failed"]["error_code"] == "execution_failed"
        assert jobs["legacy-job-future"]["status"] == "failed"
        assert _as_utc(jobs["legacy-job-future"]["updated_at"]) == (_FUTURE_UPDATED_AT)
        assert all(row["attempt_count"] == 1 for row in jobs.values())
        assert all(row["max_attempts"] == 3 for row in jobs.values())
        assert all(row["available_at"] == row["created_at"] for row in jobs.values())

        assert set(attempts) == set(jobs)
        assert attempts["legacy-job-succeeded"]["status"] == "succeeded"
        assert attempts["legacy-job-failed"]["status"] == "failed"
        assert attempts["legacy-job-queued"]["error_code"] == ("legacy_payload_missing")
        assert _as_utc(attempts["legacy-job-future"]["heartbeat_at"]) == (
            _FUTURE_UPDATED_AT
        )

        index_names = {
            item["name"] for item in inspect(engine).get_indexes("control_plane_jobs")
        }
        assert "ix_control_plane_jobs_claimable" in index_names
    finally:
        engine.dispose()


def test_head_accepts_only_the_six_job_statuses(database_url: str) -> None:
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(database_url)
    statuses = (
        ("queued", 0, None),
        ("running", 1, None),
        ("cancel_requested", 1, None),
        ("succeeded", 1, None),
        ("failed", 1, "execution_failed"),
        ("canceled", 0, None),
    )
    try:
        with engine.begin() as connection:
            for status, attempt_count, error_code in statuses:
                connection.execute(
                    insert(jobs_table).values(
                        job_id=f"status-job-{status}",
                        kind="run",
                        status=status,
                        idempotency_key=f"status-key-{status}",
                        request_digest=sha256_digest({"status": status}),
                        resource_id=f"status-run-{status}",
                        attempt_count=attempt_count,
                        max_attempts=3,
                        available_at=_CREATED_AT,
                        error_code=error_code,
                        created_at=_CREATED_AT,
                        updated_at=_CREATED_AT,
                        version=0,
                    )
                )

        with raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                insert(jobs_table).values(
                    job_id="status-job-invalid",
                    kind="run",
                    status="cancelled",
                    idempotency_key="status-key-invalid",
                    request_digest=sha256_digest({"status": "invalid"}),
                    resource_id="status-run-invalid",
                    attempt_count=0,
                    max_attempts=3,
                    available_at=_CREATED_AT,
                    error_code=None,
                    created_at=_CREATED_AT,
                    updated_at=_CREATED_AT,
                    version=0,
                )
            )
    finally:
        engine.dispose()


def test_payload_and_attempt_tables_allow_only_one_active_lease(
    database_url: str,
) -> None:
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(database_url)
    heartbeat_at = _CREATED_AT + timedelta(seconds=1)
    lease_expires_at = heartbeat_at + timedelta(seconds=30)
    try:
        with engine.begin() as connection:
            connection.execute(
                insert(jobs_table).values(
                    job_id="lease-job",
                    kind="run",
                    status="queued",
                    idempotency_key="lease-key",
                    request_digest=sha256_digest({"lease": "request"}),
                    resource_id="lease-run",
                    attempt_count=0,
                    max_attempts=3,
                    available_at=_CREATED_AT,
                    error_code=None,
                    created_at=_CREATED_AT,
                    updated_at=_CREATED_AT,
                    version=0,
                )
            )
            connection.execute(
                insert(job_payloads_table).values(
                    job_id="lease-job",
                    payload_digest=sha256_digest({"lease": "payload"}),
                    document="{}",
                    created_at=_CREATED_AT,
                )
            )
            connection.execute(
                insert(job_attempts_table).values(
                    job_id="lease-job",
                    attempt_number=1,
                    status="running",
                    worker_id="worker-a",
                    lease_token="lease-token-aaaaaaaaaaaaaaaaaaaa",
                    error_code=None,
                    started_at=_CREATED_AT,
                    heartbeat_at=heartbeat_at,
                    lease_expires_at=lease_expires_at,
                    finished_at=None,
                )
            )

        with raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                insert(job_attempts_table).values(
                    job_id="lease-job",
                    attempt_number=2,
                    status="running",
                    worker_id="worker-b",
                    lease_token="lease-token-bbbbbbbbbbbbbbbbbbbb",
                    error_code=None,
                    started_at=_CREATED_AT,
                    heartbeat_at=heartbeat_at,
                    lease_expires_at=lease_expires_at,
                    finished_at=None,
                )
            )

        with engine.begin() as connection:
            connection.execute(
                update(job_attempts_table)
                .where(
                    job_attempts_table.c.job_id == "lease-job",
                    job_attempts_table.c.attempt_number == 1,
                )
                .values(status="succeeded", finished_at=heartbeat_at)
            )
            connection.execute(
                insert(job_attempts_table).values(
                    job_id="lease-job",
                    attempt_number=2,
                    status="running",
                    worker_id="worker-b",
                    lease_token="lease-token-bbbbbbbbbbbbbbbbbbbb",
                    error_code=None,
                    started_at=_CREATED_AT,
                    heartbeat_at=heartbeat_at,
                    lease_expires_at=lease_expires_at,
                    finished_at=None,
                )
            )

        with engine.connect() as connection:
            assert len(connection.execute(select(job_attempts_table)).all()) == 2
            assert connection.execute(select(job_payloads_table)).one().job_id == (
                "lease-job"
            )

        invalid_identities = (
            ("", "c" * 32),
            ("w" * 129, "d" * 32),
            ("worker-c", "e" * 31),
            ("worker-c", "f" * 129),
        )
        for worker_id, lease_token in invalid_identities:
            with raises(IntegrityError), engine.begin() as connection:
                connection.execute(
                    insert(job_attempts_table).values(
                        job_id="lease-job",
                        attempt_number=3,
                        status="succeeded",
                        worker_id=worker_id,
                        lease_token=lease_token,
                        error_code=None,
                        started_at=_CREATED_AT,
                        heartbeat_at=heartbeat_at,
                        lease_expires_at=lease_expires_at,
                        finished_at=heartbeat_at,
                    )
                )
    finally:
        engine.dispose()


def test_downgrade_terminalizes_worker_states_before_removing_payloads(
    database_url: str,
) -> None:
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            insert(jobs_table).values(
                job_id="downgrade-canceled-job",
                kind="run",
                status="canceled",
                idempotency_key="downgrade-canceled-key",
                request_digest=sha256_digest({"downgrade": True}),
                resource_id="downgrade-canceled-run",
                attempt_count=0,
                max_attempts=3,
                available_at=_FUTURE_CREATED_AT,
                error_code=None,
                created_at=_FUTURE_CREATED_AT,
                updated_at=_FUTURE_UPDATED_AT,
                version=0,
            )
        )
    engine.dispose()

    command.downgrade(config, _REVISION_ONE)
    downgraded = create_engine(database_url)
    try:
        inspector = inspect(downgraded)
        assert "control_plane_job_attempts" not in inspector.get_table_names()
        assert "control_plane_job_payloads" not in inspector.get_table_names()
        assert {
            column["name"] for column in inspector.get_columns("control_plane_jobs")
        }.isdisjoint({"attempt_count", "max_attempts", "available_at"})
        with downgraded.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT status, error_code, updated_at "
                        "FROM control_plane_jobs "
                        "WHERE job_id = 'downgrade-canceled-job'"
                    )
                )
                .mappings()
                .one()
            )
        assert row["status"] == "failed"
        assert row["error_code"] == "worker_state_removed"
        assert _as_utc(datetime.fromisoformat(row["updated_at"])) == (
            _FUTURE_UPDATED_AT
        )
    finally:
        downgraded.dispose()

    command.upgrade(config, _HEAD)
    upgraded = create_engine(database_url)
    try:
        with upgraded.connect() as connection:
            context = MigrationContext.configure(connection)
            assert compare_metadata(context, CONTROL_PLANE_METADATA) == []
    finally:
        upgraded.dispose()
