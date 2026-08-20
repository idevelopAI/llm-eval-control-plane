from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from math import inf, nan
from typing import cast
from uuid import UUID

from pydantic import SecretStr
from pytest import mark, raises

from llm_eval_control_plane.adapters.postgres_sandbox import (
    ConnectFactory,
    PostgresReplayError,
    PostgresSandboxConfig,
    PostgresSandboxLimits,
    PsycopgPostgresExecutor,
    normalize_postgres_value,
)
from llm_eval_control_plane.domain.sql import SqlReplayResult

FIXTURE_DIGEST = "sha256:" + ("a" * 64)


@dataclass(frozen=True)
class Description:
    name: str


class FakeDatabaseError(RuntimeError):
    def __init__(self, message: str, *, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class FakeCursor:
    def __init__(
        self,
        *,
        columns: tuple[str, ...] = ("id",),
        rows: tuple[tuple[object, ...], ...] = ((1,),),
        failure: Exception | None = None,
    ) -> None:
        self.description = tuple(Description(name) for name in columns)
        self.rows = rows
        self.failure = failure
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []
        self.closed = False

    def execute(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> object:
        self.executed.append((query, params))
        if self.failure is not None and not query.startswith(
            ("BEGIN", "SELECT set_config")
        ):
            raise self.failure
        return None

    def fetchmany(self, size: int) -> tuple[tuple[object, ...], ...]:
        return self.rows[:size]

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self.fake_cursor = cursor
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.fake_cursor

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class FakeConnect:
    def __init__(
        self,
        connections: list[FakeConnection],
        *,
        failure: Exception | None = None,
    ) -> None:
        self.connections = connections
        self.failure = failure
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> FakeConnection:
        self.calls.append((args, kwargs))
        if self.failure is not None:
            raise self.failure
        return self.connections[len(self.calls) - 1]


def config(*, limits: PostgresSandboxLimits | None = None) -> PostgresSandboxConfig:
    return PostgresSandboxConfig(
        dsn=SecretStr("postgresql://readonly:secret@localhost/databridge"),
        fixture_digest=FIXTURE_DIGEST,
        limits=limits or PostgresSandboxLimits(),
    )


def connect_factory(connect: FakeConnect) -> ConnectFactory:
    return cast(ConnectFactory, connect)


def test_executor_uses_fresh_explicit_read_only_transaction_and_original_sql() -> None:
    source = "SELECT id FROM employees ORDER BY id"
    first_cursor = FakeCursor(rows=((1,), (2,)))
    second_cursor = FakeCursor(rows=((1,), (2,)))
    connections = [FakeConnection(first_cursor), FakeConnection(second_cursor)]
    connect = FakeConnect(connections)
    executor = PsycopgPostgresExecutor(config(), connect=connect_factory(connect))

    first = executor.execute(source)
    second = executor.execute(source)

    assert first == second == SqlReplayResult(columns=("id",), rows=((1,), (2,)))
    assert len(connect.calls) == 2
    assert all(call[1]["autocommit"] is True for call in connect.calls)
    assert first_cursor.executed[0] == ("BEGIN TRANSACTION READ ONLY", None)
    assert first_cursor.executed[-1] == (source, None)
    assert first_cursor.executed.count((source, None)) == 1
    assert all(connection.rolled_back for connection in connections)
    assert all(connection.closed for connection in connections)
    assert first_cursor.closed and second_cursor.closed


def test_failed_policy_never_opens_a_connection() -> None:
    connect = FakeConnect([])
    executor = PsycopgPostgresExecutor(config(), connect=connect_factory(connect))

    with raises(PostgresReplayError) as captured:
        executor.execute("DELETE FROM employees")

    assert captured.value.code == "policy_rejected"
    assert connect.calls == []


def test_connection_and_query_errors_are_sanitized() -> None:
    leaked_dsn = "postgresql://admin:super-secret@internal/private"
    connect_failure = FakeConnect([], failure=RuntimeError(leaked_dsn))
    executor = PsycopgPostgresExecutor(
        config(), connect=connect_factory(connect_failure)
    )

    with raises(PostgresReplayError) as captured:
        executor.execute("SELECT id FROM employees")

    assert captured.value.code == "connection_failed"
    assert leaked_dsn not in str(captured.value)

    cursor = FakeCursor(failure=FakeDatabaseError(leaked_dsn))
    connection = FakeConnection(cursor)
    executor = PsycopgPostgresExecutor(
        config(), connect=connect_factory(FakeConnect([connection]))
    )
    with raises(PostgresReplayError) as captured:
        executor.execute("SELECT id FROM employees")
    assert captured.value.code == "query_failed"
    assert leaked_dsn not in str(captured.value)
    assert connection.rolled_back and connection.closed


def test_statement_timeout_has_a_stable_code() -> None:
    cursor = FakeCursor(
        failure=FakeDatabaseError("raw database message", sqlstate="57014")
    )
    executor = PsycopgPostgresExecutor(
        config(), connect=connect_factory(FakeConnect([FakeConnection(cursor)]))
    )

    with raises(PostgresReplayError) as captured:
        executor.execute("SELECT id FROM employees")

    assert captured.value.code == "statement_timeout"
    assert "raw database message" not in str(captured.value)


@mark.parametrize(
    ("limits", "columns", "rows", "code"),
    [
        (
            PostgresSandboxLimits(max_rows=1),
            ("id",),
            ((1,), (2,)),
            "row_limit_exceeded",
        ),
        (
            PostgresSandboxLimits(max_cells=1),
            ("id", "name"),
            ((1, "Ada"),),
            "cell_limit_exceeded",
        ),
        (
            PostgresSandboxLimits(max_columns=1),
            ("id", "name"),
            ((1, "Ada"),),
            "invalid_result_shape",
        ),
        (
            PostgresSandboxLimits(max_result_bytes=10),
            ("name",),
            (("a long result",),),
            "byte_limit_exceeded",
        ),
    ],
)
def test_executor_enforces_result_caps(
    limits: PostgresSandboxLimits,
    columns: tuple[str, ...],
    rows: tuple[tuple[object, ...], ...],
    code: str,
) -> None:
    cursor = FakeCursor(columns=columns, rows=rows)
    executor = PsycopgPostgresExecutor(
        config(limits=limits),
        connect=connect_factory(FakeConnect([FakeConnection(cursor)])),
    )

    with raises(PostgresReplayError) as captured:
        executor.execute("SELECT id FROM employees")

    assert captured.value.code == code


def test_executor_rejects_duplicate_columns_and_wrong_row_width() -> None:
    for cursor in (
        FakeCursor(columns=("id", "id"), rows=((1, 1),)),
        FakeCursor(columns=("id", "name"), rows=((1,),)),
    ):
        executor = PsycopgPostgresExecutor(
            config(), connect=connect_factory(FakeConnect([FakeConnection(cursor)]))
        )
        with raises(PostgresReplayError) as captured:
            executor.execute("SELECT id FROM employees")
        assert captured.value.code == "invalid_result_shape"


def test_postgres_value_normalization_is_deterministic() -> None:
    identifier = UUID("7ad78685-a3e5-4b8c-9141-769ff8c94c02")
    values = (
        None,
        True,
        7,
        1.5,
        Decimal("42.00"),
        Decimal("87166.67"),
        date(2026, 1, 2),
        time(3, 4, 5, 6000),
        datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
        datetime(2026, 1, 2, 4, 4, tzinfo=timezone(timedelta(hours=1))),
        identifier,
    )

    assert tuple(normalize_postgres_value(value) for value in values) == (
        None,
        True,
        7,
        1.5,
        42,
        87166.67,
        "2026-01-02",
        "03:04:05.006000",
        "2026-01-02T03:04:00Z",
        "2026-01-02T03:04:00Z",
        str(identifier),
    )


@mark.parametrize(
    "value",
    [nan, inf, Decimal("NaN"), Decimal("1.0000000000000000001"), 2**60, b"x"],
)
def test_postgres_value_normalization_rejects_unsafe_values(value: object) -> None:
    with raises(PostgresReplayError) as captured:
        normalize_postgres_value(value)
    assert captured.value.code == "unsupported_value"


def test_config_and_executor_repr_redact_the_dsn() -> None:
    sandbox_config = config()
    executor = PsycopgPostgresExecutor(
        sandbox_config,
        connect=connect_factory(FakeConnect([FakeConnection(FakeCursor())])),
    )

    assert "secret" not in repr(sandbox_config)
    assert "secret" not in repr(executor)
    assert FIXTURE_DIGEST in repr(executor)


def test_executor_semantic_config_covers_limits_without_secrets() -> None:
    executor = PsycopgPostgresExecutor(
        config(limits=PostgresSandboxLimits(max_rows=17)),
        connect=connect_factory(FakeConnect([FakeConnection(FakeCursor())])),
    )

    semantic_config = executor.semantic_config
    assert semantic_config["fixture_digest"] == FIXTURE_DIGEST
    assert semantic_config["limits"] == {
        "connect_timeout_seconds": 5,
        "lock_timeout_ms": 250,
        "max_cells": 25_000,
        "max_columns": 64,
        "max_result_bytes": 1_048_576,
        "max_rows": 17,
        "statement_timeout_ms": 2_000,
    }
    assert semantic_config["transaction"] == {
        "read_only": True,
        "rollback_always": True,
        "search_path": "public",
        "timezone": "UTC",
    }
    assert "secret" not in repr(semantic_config)


def test_fixture_fingerprint_is_order_independent_and_content_sensitive() -> None:
    class FingerprintExecutor(PsycopgPostgresExecutor):
        def __init__(self, changed: bool) -> None:
            super().__init__(
                config(),
                connect=connect_factory(FakeConnect([FakeConnection(FakeCursor())])),
            )
            self.changed = changed

        def execute(self, sql: str) -> SqlReplayResult:
            if "departments" in sql:
                rows = ((2, "Sales"), (1, "Engineering"))
                if self.changed:
                    rows = ((2, "Changed"), (1, "Engineering"))
                return SqlReplayResult(columns=("id", "name"), rows=rows)
            return SqlReplayResult(columns=("id",), rows=((2,), (1,)))

    original = FingerprintExecutor(False).fingerprint_fixture()
    repeated = FingerprintExecutor(False).fingerprint_fixture()
    changed = FingerprintExecutor(True).fingerprint_fixture()

    assert original == repeated
    assert changed != original
