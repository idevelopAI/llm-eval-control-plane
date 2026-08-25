"""Bounded PostgreSQL replay in fresh read-only transactions."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import UTC, date, datetime, time
from decimal import Decimal
from importlib.metadata import version
from typing import Annotated, ClassVar, Protocol, cast
from uuid import UUID

from pydantic import Field, PositiveInt, SecretStr

from llm_eval_control_plane.adapters.sql_policy import PostgresSqlPolicy
from llm_eval_control_plane.domain.artifacts import Sha256Digest
from llm_eval_control_plane.domain.canonical import (
    canonical_json_bytes,
    sha256_digest,
)
from llm_eval_control_plane.domain.execution import SafeCode
from llm_eval_control_plane.domain.models import FrozenModel
from llm_eval_control_plane.domain.sql import SqlReplayResult, SqlRow, SqlScalar


class PostgresReplayError(RuntimeError):
    """Content-safe replay failure that never retains SQL or database messages."""

    _MESSAGES: ClassVar[dict[str, str]] = {
        "byte_limit_exceeded": "Replay result exceeded the byte limit",
        "cell_limit_exceeded": "Replay result exceeded the cell limit",
        "connection_failed": "Replay database connection failed",
        "dependency_unavailable": "PostgreSQL replay dependency is unavailable",
        "invalid_result_shape": "Replay returned an invalid result shape",
        "policy_rejected": "Replay SQL did not pass the read-only policy",
        "query_failed": "Replay query failed",
        "row_limit_exceeded": "Replay result exceeded the row limit",
        "statement_timeout": "Replay query exceeded its time limit",
        "unsupported_value": "Replay returned an unsupported value",
    }

    def __init__(self, code: SafeCode) -> None:
        if code not in self._MESSAGES:
            raise ValueError("unknown PostgreSQL replay error code")
        super().__init__(self._MESSAGES[code])
        self.code = code


class PostgresSandboxLimits(FrozenModel):
    """Resource limits applied independently to every replay query."""

    statement_timeout_ms: Annotated[PositiveInt, Field(le=60_000)] = 2_000
    lock_timeout_ms: Annotated[PositiveInt, Field(le=10_000)] = 250
    connect_timeout_seconds: Annotated[PositiveInt, Field(le=30)] = 5
    max_rows: Annotated[PositiveInt, Field(le=10_000)] = 1_000
    max_columns: Annotated[PositiveInt, Field(le=256)] = 64
    max_cells: Annotated[PositiveInt, Field(le=100_000)] = 25_000
    max_result_bytes: Annotated[PositiveInt, Field(le=16_777_216)] = 1_048_576


class PostgresSandboxConfig(FrozenModel):
    """Connection secret and immutable fixture identity for the sandbox."""

    dsn: SecretStr = Field(repr=False)
    fixture_digest: Sha256Digest
    limits: PostgresSandboxLimits = PostgresSandboxLimits()


class PostgresExecutor(Protocol):
    """Replay protocol injected into the deterministic composite evaluator."""

    @property
    def fixture_digest(self) -> Sha256Digest: ...

    @property
    def semantic_config(self) -> dict[str, object]: ...

    def execute(self, sql: str) -> SqlReplayResult: ...

    def fingerprint_fixture(self) -> Sha256Digest: ...


class _Description(Protocol):
    name: str


class _Cursor(Protocol):
    description: Sequence[_Description] | None

    def execute(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> object: ...

    def fetchmany(self, size: int) -> Sequence[Sequence[object]]: ...

    def close(self) -> None: ...


class _Connection(Protocol):
    def cursor(self) -> _Cursor: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


ConnectFactory = Callable[..., _Connection]
_MAX_SAFE_INTEGER = (2**53) - 1


def normalize_postgres_value(value: object) -> SqlScalar:
    """Map supported psycopg values to deterministic JSON scalar evidence."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise PostgresReplayError("unsupported_value")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PostgresReplayError("unsupported_value")
        return 0.0 if value == 0.0 else value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise PostgresReplayError("unsupported_value")
        normalized = value.normalize()
        if normalized == normalized.to_integral_value():
            integer = int(normalized)
            if abs(integer) > _MAX_SAFE_INTEGER:
                raise PostgresReplayError("unsupported_value")
            return integer
        floating = float(normalized)
        if not math.isfinite(floating) or Decimal(str(floating)) != normalized:
            raise PostgresReplayError("unsupported_value")
        return floating
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
            return value.isoformat().replace("+00:00", "Z")
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise PostgresReplayError("unsupported_value")


class PsycopgPostgresExecutor:
    """Execute original SQL with psycopg under bounded read-only transactions."""

    def __init__(
        self,
        config: PostgresSandboxConfig,
        *,
        policy: PostgresSqlPolicy | None = None,
        connect: ConnectFactory | None = None,
    ) -> None:
        self._config = config
        self._policy = policy or PostgresSqlPolicy()
        self._connect = connect or self._load_connect()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(fixture_digest={self.fixture_digest!r}, "
            f"limits={self._config.limits!r})"
        )

    @property
    def fixture_digest(self) -> Sha256Digest:
        return self._config.fixture_digest

    @property
    def semantic_config(self) -> dict[str, object]:
        """Return non-secret replay settings covered by evaluator identity."""
        return {
            "driver": {"name": "psycopg", "version": version("psycopg")},
            "executor_schema": "psycopg-postgres-read-only/v1",
            "fixture_digest": self.fixture_digest,
            "limits": self._config.limits.model_dump(mode="json"),
            "transaction": {
                "read_only": True,
                "rollback_always": True,
                "search_path": "public",
                "timezone": "UTC",
            },
        }

    @staticmethod
    def _load_connect() -> ConnectFactory:
        try:
            import psycopg
        except ImportError:

            def unavailable(*_args: object, **_kwargs: object) -> _Connection:
                raise PostgresReplayError("dependency_unavailable")

            return unavailable
        return cast(ConnectFactory, psycopg.connect)

    def execute(self, sql: str) -> SqlReplayResult:
        decision = self._policy.evaluate(sql)
        if not decision.allowed:
            raise PostgresReplayError("policy_rejected")

        connection: _Connection | None = None
        cursor: _Cursor | None = None
        try:
            connection = self._connect(
                self._config.dsn.get_secret_value(),
                autocommit=True,
                connect_timeout=self._config.limits.connect_timeout_seconds,
            )
        except PostgresReplayError:
            raise
        except Exception:
            raise PostgresReplayError("connection_failed") from None
        try:
            cursor = connection.cursor()
            cursor.execute("BEGIN TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (f"{self._config.limits.statement_timeout_ms}ms",),
            )
            cursor.execute(
                "SELECT set_config('lock_timeout', %s, true)",
                (f"{self._config.limits.lock_timeout_ms}ms",),
            )
            cursor.execute("SELECT set_config('TimeZone', 'UTC', true)")
            cursor.execute("SELECT set_config('search_path', 'public', true)")
            cursor.execute(sql)
            result = self._read_result(cursor)
        except PostgresReplayError:
            raise
        except Exception as error:
            code = (
                "statement_timeout"
                if getattr(error, "sqlstate", None) == "57014"
                else "query_failed"
            )
            raise PostgresReplayError(code) from None
        finally:
            if connection is not None:
                with suppress(Exception):
                    connection.rollback()
            if cursor is not None:
                with suppress(Exception):
                    cursor.close()
            if connection is not None:
                with suppress(Exception):
                    connection.close()
        return result

    def fingerprint_fixture(self) -> Sha256Digest:
        """Hash bounded rows and column metadata without returning fixture data."""
        tables: list[dict[str, object]] = []
        for table_name in ("departments", "employees", "projects"):
            # table_name is selected exclusively from the fixed tuple above.
            result = self.execute(f"SELECT * FROM public.{table_name}")  # noqa: S608
            rows = sorted(result.rows, key=lambda row: canonical_json_bytes(list(row)))
            tables.append(
                {
                    "columns": list(result.columns),
                    "name": table_name,
                    "rows": [list(row) for row in rows],
                }
            )
        return sha256_digest(
            {
                "fingerprint_schema": "databridge-postgres-fixture/v1",
                "tables": tables,
            }
        )

    def _read_result(self, cursor: _Cursor) -> SqlReplayResult:
        description = cursor.description
        if description is None or len(description) > self._config.limits.max_columns:
            raise PostgresReplayError("invalid_result_shape")
        columns = tuple(item.name for item in description)
        if any(not isinstance(column, str) or not column for column in columns):
            raise PostgresReplayError("invalid_result_shape")
        if len(columns) != len(set(columns)):
            raise PostgresReplayError("invalid_result_shape")

        raw_rows = cursor.fetchmany(self._config.limits.max_rows + 1)
        if len(raw_rows) > self._config.limits.max_rows:
            raise PostgresReplayError("row_limit_exceeded")
        if len(raw_rows) * len(columns) > self._config.limits.max_cells:
            raise PostgresReplayError("cell_limit_exceeded")

        rows: list[SqlRow] = []
        for raw_row in raw_rows:
            if len(raw_row) != len(columns):
                raise PostgresReplayError("invalid_result_shape")
            rows.append(tuple(normalize_postgres_value(value) for value in raw_row))
        content = {
            "columns": list(columns),
            "rows": [list(row) for row in rows],
        }
        if len(canonical_json_bytes(content)) > self._config.limits.max_result_bytes:
            raise PostgresReplayError("byte_limit_exceeded")
        try:
            return SqlReplayResult(columns=columns, rows=tuple(rows))
        except ValueError:
            raise PostgresReplayError("invalid_result_shape") from None
