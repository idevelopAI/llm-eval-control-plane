"""SQLAlchemy persistence for durable control-plane metadata and evidence."""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from llm_eval_control_plane.application.control_plane import (
    ClaimedJob,
    ControlPlaneStoreError,
    StoreConflictError,
    StoreIdempotencyConflictError,
    StoreInvalidCursorError,
    StoreLeaseLostError,
    StoreNotFoundError,
    StoreTransitionError,
)
from llm_eval_control_plane.domain.canonical import (
    CanonicalJsonError,
    JsonValue,
    canonical_json_bytes,
    parse_json,
)
from llm_eval_control_plane.domain.comparison import ReleaseDecision, ReleaseStatus
from llm_eval_control_plane.domain.control_plane import (
    CursorPage,
    DatasetListRecord,
    DatasetRecord,
    JobAttemptRecord,
    JobAttemptStatus,
    JobKind,
    JobPayload,
    JobRecord,
    JobStatus,
    LeaseToken,
    ReleaseDecisionListRecord,
    ReleaseDecisionRecord,
    RunListRecord,
    RunRecord,
    WorkerId,
)
from llm_eval_control_plane.domain.datasets import DatasetVersion
from llm_eval_control_plane.domain.results import RunResult

CONTROL_PLANE_METADATA = MetaData()

datasets_table = Table(
    "control_plane_datasets",
    CONTROL_PLANE_METADATA,
    Column("name", String(128), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("digest", String(71), nullable=False),
    Column("case_count", Integer, nullable=False),
    Column("document", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("revision > 0", name="ck_control_plane_datasets_revision"),
    CheckConstraint("case_count > 0", name="ck_control_plane_datasets_case_count"),
    CheckConstraint(
        "length(digest) = 71", name="ck_control_plane_datasets_digest_length"
    ),
    PrimaryKeyConstraint("name", "revision", name="pk_control_plane_datasets"),
)
Index(
    "ix_control_plane_datasets_created_name_revision",
    datasets_table.c.created_at,
    datasets_table.c.name,
    datasets_table.c.revision,
)
Index(
    "ix_control_plane_datasets_name_created_revision",
    datasets_table.c.name,
    datasets_table.c.created_at,
    datasets_table.c.revision,
)
Index("ix_control_plane_datasets_digest", datasets_table.c.digest)

jobs_table = Table(
    "control_plane_jobs",
    CONTROL_PLANE_METADATA,
    Column("job_id", String(128), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("request_digest", String(71), nullable=False),
    Column("resource_id", String(128), nullable=False),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False, server_default=text("3")),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("error_code", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("version", Integer, nullable=False, server_default=text("0")),
    CheckConstraint("kind IN ('run', 'comparison')", name="ck_control_plane_jobs_kind"),
    PrimaryKeyConstraint("job_id", name="pk_control_plane_jobs"),
    CheckConstraint(
        "status IN ('queued', 'running', 'cancel_requested', "
        "'succeeded', 'failed', 'canceled')",
        name="ck_control_plane_jobs_status",
    ),
    CheckConstraint(
        "attempt_count >= 0 AND attempt_count <= max_attempts",
        name="ck_control_plane_jobs_attempt_count",
    ),
    CheckConstraint(
        "max_attempts BETWEEN 1 AND 10",
        name="ck_control_plane_jobs_max_attempts",
    ),
    CheckConstraint(
        "status <> 'queued' OR attempt_count < max_attempts",
        name="ck_control_plane_jobs_queued_attempt",
    ),
    CheckConstraint(
        "status NOT IN ('running', 'cancel_requested', 'succeeded', 'failed') "
        "OR attempt_count > 0",
        name="ck_control_plane_jobs_started_attempt",
    ),
    CheckConstraint(
        "available_at >= created_at",
        name="ck_control_plane_jobs_available_at",
    ),
    CheckConstraint(
        "updated_at >= created_at",
        name="ck_control_plane_jobs_updated_at",
    ),
    CheckConstraint("version >= 0", name="ck_control_plane_jobs_version"),
    CheckConstraint(
        "(status = 'failed' AND error_code IS NOT NULL) OR "
        "(status <> 'failed' AND error_code IS NULL)",
        name="ck_control_plane_jobs_failure_code",
    ),
    UniqueConstraint(
        "kind",
        "idempotency_key",
        name="uq_control_plane_jobs_kind_idempotency_key",
    ),
    UniqueConstraint(
        "kind", "resource_id", name="uq_control_plane_jobs_kind_resource_id"
    ),
)
Index(
    "ix_control_plane_jobs_status_created_job_id",
    jobs_table.c.status,
    jobs_table.c.created_at,
    jobs_table.c.job_id,
)
Index(
    "ix_control_plane_jobs_created_job_id",
    jobs_table.c.created_at,
    jobs_table.c.job_id,
)
Index(
    "ix_control_plane_jobs_kind_created_job_id",
    jobs_table.c.kind,
    jobs_table.c.created_at,
    jobs_table.c.job_id,
)
Index(
    "ix_control_plane_jobs_kind_status_created_job_id",
    jobs_table.c.kind,
    jobs_table.c.status,
    jobs_table.c.created_at,
    jobs_table.c.job_id,
)
Index(
    "ix_control_plane_jobs_claimable",
    jobs_table.c.available_at,
    jobs_table.c.created_at,
    jobs_table.c.job_id,
    postgresql_where=text("status = 'queued'"),
    sqlite_where=text("status = 'queued'"),
)

job_payloads_table = Table(
    "control_plane_job_payloads",
    CONTROL_PLANE_METADATA,
    Column("job_id", String(128), nullable=False),
    Column("payload_digest", String(71), nullable=False),
    Column("document", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ("job_id",),
        ("control_plane_jobs.job_id",),
        name="fk_control_plane_job_payloads_job",
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "length(payload_digest) = 71",
        name="ck_control_plane_job_payloads_digest_length",
    ),
    PrimaryKeyConstraint("job_id", name="pk_control_plane_job_payloads"),
)

job_attempts_table = Table(
    "control_plane_job_attempts",
    CONTROL_PLANE_METADATA,
    Column("job_id", String(128), nullable=False),
    Column("attempt_number", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("worker_id", String(128), nullable=False),
    Column("lease_token", String(128), nullable=False),
    Column("error_code", String(64), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("heartbeat_at", DateTime(timezone=True), nullable=False),
    Column("lease_expires_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    ForeignKeyConstraint(
        ("job_id",),
        ("control_plane_jobs.job_id",),
        name="fk_control_plane_job_attempts_job",
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "attempt_number > 0",
        name="ck_control_plane_job_attempts_attempt_number",
    ),
    CheckConstraint(
        "length(worker_id) BETWEEN 1 AND 128",
        name="ck_control_plane_job_attempts_worker_id_length",
    ),
    CheckConstraint(
        "length(lease_token) BETWEEN 32 AND 128",
        name="ck_control_plane_job_attempts_lease_token_length",
    ),
    CheckConstraint(
        "status IN ('running', 'succeeded', 'retry_scheduled', 'failed', "
        "'canceled', 'lease_expired')",
        name="ck_control_plane_job_attempts_status",
    ),
    CheckConstraint(
        "heartbeat_at >= started_at",
        name="ck_control_plane_job_attempts_heartbeat_at",
    ),
    CheckConstraint(
        "lease_expires_at > heartbeat_at",
        name="ck_control_plane_job_attempts_lease_expires_at",
    ),
    CheckConstraint(
        "finished_at IS NULL OR finished_at >= heartbeat_at",
        name="ck_control_plane_job_attempts_finished_at",
    ),
    CheckConstraint(
        "(status = 'running' AND finished_at IS NULL) OR "
        "(status <> 'running' AND finished_at IS NOT NULL)",
        name="ck_control_plane_job_attempts_terminal_time",
    ),
    CheckConstraint(
        "(status IN ('retry_scheduled', 'failed', 'lease_expired') "
        "AND error_code IS NOT NULL) OR "
        "(status NOT IN ('retry_scheduled', 'failed', 'lease_expired') "
        "AND error_code IS NULL)",
        name="ck_control_plane_job_attempts_failure_code",
    ),
    PrimaryKeyConstraint(
        "job_id",
        "attempt_number",
        name="pk_control_plane_job_attempts",
    ),
    UniqueConstraint(
        "job_id",
        "lease_token",
        name="uq_control_plane_job_attempts_job_lease_token",
    ),
)
Index(
    "ux_control_plane_job_attempts_active_job",
    job_attempts_table.c.job_id,
    unique=True,
    postgresql_where=text("status = 'running'"),
    sqlite_where=text("status = 'running'"),
)
Index(
    "ix_control_plane_job_attempts_expiring",
    job_attempts_table.c.lease_expires_at,
    job_attempts_table.c.job_id,
    job_attempts_table.c.attempt_number,
    postgresql_where=text("status = 'running'"),
    sqlite_where=text("status = 'running'"),
)

runs_table = Table(
    "control_plane_runs",
    CONTROL_PLANE_METADATA,
    Column("run_id", String(128), nullable=False),
    Column("result_digest", String(71), nullable=False),
    Column("dataset_name", String(128), nullable=False),
    Column("dataset_revision", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("execution_mode", String(32), nullable=False),
    Column("document", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ("dataset_name", "dataset_revision"),
        (
            "control_plane_datasets.name",
            "control_plane_datasets.revision",
        ),
        name="fk_control_plane_runs_dataset",
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "dataset_revision > 0", name="ck_control_plane_runs_dataset_revision"
    ),
    CheckConstraint(
        "status IN ('completed', 'completed_with_failures')",
        name="ck_control_plane_runs_status",
    ),
    CheckConstraint(
        "execution_mode IN ('offline_deterministic_fixture', 'offline_mock', 'live')",
        name="ck_control_plane_runs_execution_mode",
    ),
    PrimaryKeyConstraint("run_id", name="pk_control_plane_runs"),
    CheckConstraint(
        "length(result_digest) = 71",
        name="ck_control_plane_runs_result_digest_length",
    ),
)
Index(
    "ix_control_plane_runs_dataset_created_run_id",
    runs_table.c.dataset_name,
    runs_table.c.created_at,
    runs_table.c.run_id,
)
Index(
    "ix_control_plane_runs_created_run_id",
    runs_table.c.created_at,
    runs_table.c.run_id,
)
Index("ix_control_plane_runs_result_digest", runs_table.c.result_digest)

release_decisions_table = Table(
    "control_plane_release_decisions",
    CONTROL_PLANE_METADATA,
    Column("decision_id", String(128), nullable=False),
    Column("decision_digest", String(71), nullable=False),
    Column("baseline_run_id", String(128), nullable=False),
    Column("candidate_run_id", String(128), nullable=False),
    Column("status", String(16), nullable=False),
    Column("document", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ("baseline_run_id",),
        ("control_plane_runs.run_id",),
        name="fk_control_plane_release_decisions_baseline_run",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ("candidate_run_id",),
        ("control_plane_runs.run_id",),
        name="fk_control_plane_release_decisions_candidate_run",
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "status IN ('passed', 'failed')",
        name="ck_control_plane_release_decisions_status",
    ),
    PrimaryKeyConstraint("decision_id", name="pk_control_plane_release_decisions"),
    CheckConstraint(
        "length(decision_digest) = 71",
        name="ck_control_plane_release_decisions_digest_length",
    ),
)
Index(
    "ix_control_plane_release_decisions_status_created_decision_id",
    release_decisions_table.c.status,
    release_decisions_table.c.created_at,
    release_decisions_table.c.decision_id,
)
Index(
    "ix_control_plane_release_decisions_created_decision_id",
    release_decisions_table.c.created_at,
    release_decisions_table.c.decision_id,
)
Index(
    "ix_control_plane_release_decisions_decision_digest",
    release_decisions_table.c.decision_digest,
)


class ControlPlaneRepositoryError(ControlPlaneStoreError):
    """Base class for sanitized control-plane persistence failures."""


class RecordNotFoundError(ControlPlaneRepositoryError, StoreNotFoundError):
    """Raised when a requested control-plane record does not exist."""


class ImmutableRecordConflictError(ControlPlaneRepositoryError, StoreConflictError):
    """Raised when an immutable key already names different canonical bytes."""


class IdempotencyConflictError(
    ControlPlaneRepositoryError, StoreIdempotencyConflictError
):
    """Raised when an idempotency key is reused for a different request."""


class ResourceAlreadySubmittedError(ControlPlaneRepositoryError, StoreConflictError):
    """Raised when another key already claimed an explicit resource ID."""


class InvalidCursorError(ControlPlaneRepositoryError, StoreInvalidCursorError):
    """Raised when a pagination cursor is malformed or used with other filters."""


class ConcurrentTransitionError(ControlPlaneRepositoryError, StoreTransitionError):
    """Raised when another writer wins a job compare-and-set transition."""


class IllegalJobTransitionError(ControlPlaneRepositoryError, StoreTransitionError):
    """Raised when a requested lifecycle transition is not legal."""


class LeaseLostError(ControlPlaneRepositoryError, StoreLeaseLostError):
    """Raised when a worker no longer owns an active unexpired lease."""


class CorruptRecordError(ControlPlaneRepositoryError):
    """Raised when stored canonical evidence fails integrity validation."""


class PayloadTooLargeError(ControlPlaneRepositoryError):
    """Raised before persistence when canonical evidence exceeds its bound."""


Model = TypeVar("Model", bound=BaseModel)


def _model_text(model: BaseModel) -> str:
    return canonical_json_bytes(
        model.model_dump(
            mode="json",
            by_alias=False,
            exclude_defaults=False,
            exclude_none=False,
            exclude_unset=False,
        )
    ).decode("utf-8")


def _validated_model(
    document: object, model: type[Model], *, max_document_bytes: int
) -> Model:
    try:
        if not isinstance(document, str):
            raise TypeError("record document must be text")
        if len(document.encode("utf-8")) > max_document_bytes:
            raise ValueError("record document exceeds its size bound")
        value = parse_json(document)
        if canonical_json_bytes(value).decode("utf-8") != document:
            raise ValueError("record document is not canonical")
        return model.model_validate(value)
    except (
        CanonicalJsonError,
        TypeError,
        ValidationError,
        UnicodeError,
        ValueError,
    ) as error:
        raise CorruptRecordError("Stored control-plane evidence is invalid") from error


def _aware(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise CorruptRecordError("Stored control-plane timestamp is invalid")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("page limit must be between 1 and 100")
    return value


_CURSOR_DOMAIN = b"llm-eval-control-plane/keyset-cursor/v1\0"
_SCHEMA_REVISION = "20260823_0002"
_DEFAULT_MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
_MAX_JOB_PAYLOAD_BYTES = 4 * 1024 * 1024
_MIGRATION_WORKER_ID = "phase5-migration"
_MIGRATION_LEASE_TOKEN = "phase5-migration-token-000000000000"
_SAFE_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_WORKER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LEASE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_JOB_PAYLOAD_ADAPTER: TypeAdapter[JobPayload] = TypeAdapter(JobPayload)


def _bounded_int(value: int, *, lower: int, upper: int, name: str) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{name} is outside its supported range")
    return value


def _safe_code(value: str) -> str:
    if not isinstance(value, str) or _SAFE_CODE_PATTERN.fullmatch(value) is None:
        raise ValueError("error code is invalid")
    return value


def _lease_token_value(value: LeaseToken) -> str:
    if not isinstance(value, str) or _LEASE_TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError("lease token is invalid")
    return value


def _worker_id_value(value: WorkerId) -> str:
    if not isinstance(value, str) or _WORKER_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("worker identity is invalid")
    return value


def _encode_cursor(
    *,
    stream: str,
    filters: Mapping[str, JsonValue],
    key: list[JsonValue],
) -> str:
    payload: dict[str, JsonValue] = {
        "filters": dict(filters),
        "key": key,
        "stream": stream,
        "version": 1,
    }
    payload_bytes = canonical_json_bytes(payload)
    envelope = canonical_json_bytes(
        {
            "checksum": sha256(_CURSOR_DOMAIN + payload_bytes).hexdigest(),
            "payload": payload,
        }
    )
    return base64.urlsafe_b64encode(envelope).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str,
    *,
    stream: str,
    filters: Mapping[str, JsonValue],
) -> list[JsonValue]:
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
            raise ValueError("cursor length is invalid")
        if any(character not in _CURSOR_ALPHABET for character in cursor):
            raise ValueError("cursor alphabet is invalid")
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        envelope = parse_json(raw.decode("utf-8"))
        if raw != canonical_json_bytes(envelope):
            raise ValueError("cursor envelope is not canonical")
        if not isinstance(envelope, dict) or set(envelope) != {
            "checksum",
            "payload",
        }:
            raise ValueError("cursor envelope is invalid")
        payload = envelope["payload"]
        checksum = envelope["checksum"]
        if not isinstance(payload, dict) or set(payload) != {
            "filters",
            "key",
            "stream",
            "version",
        }:
            raise ValueError("cursor payload is invalid")
        payload_bytes = canonical_json_bytes(payload)
        expected = sha256(_CURSOR_DOMAIN + payload_bytes).hexdigest()
        if checksum != expected:
            raise ValueError("cursor checksum is invalid")
        if (
            payload["version"] != 1
            or payload["stream"] != stream
            or payload["filters"] != dict(filters)
            or not isinstance(payload["key"], list)
        ):
            raise ValueError("cursor context is invalid")
        return payload["key"]
    except (
        CanonicalJsonError,
        OverflowError,
        RecursionError,
        UnicodeError,
        ValueError,
    ) as error:
        raise InvalidCursorError("Pagination cursor is invalid") from error


_CURSOR_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def _cursor_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _decoded_cursor_time(value: JsonValue) -> datetime:
    try:
        if not isinstance(value, str):
            raise ValueError("cursor time must be text")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("cursor time must include a timezone")
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError) as error:
        raise InvalidCursorError("Pagination cursor is invalid") from error


class SqlAlchemyControlPlaneRepository:
    """Persist immutable evidence and atomically claim idempotent jobs."""

    def __init__(
        self,
        engine: Engine,
        *,
        max_document_bytes: int = _DEFAULT_MAX_DOCUMENT_BYTES,
    ) -> None:
        if type(max_document_bytes) is not int or max_document_bytes <= 0:
            raise ValueError("maximum document size must be a positive integer")
        self._engine = engine
        self._max_document_bytes = max_document_bytes

    def _require_document_size(self, document: str) -> None:
        if len(document.encode("utf-8")) > self._max_document_bytes:
            raise PayloadTooLargeError(
                "Canonical evidence exceeds the configured size limit"
            )

    @staticmethod
    def _require_payload_size(document: str) -> None:
        if len(document.encode("utf-8")) > _MAX_JOB_PAYLOAD_BYTES:
            raise PayloadTooLargeError(
                "Canonical worker payload exceeds its size limit"
            )

    def check_health(self) -> None:
        try:
            with self._engine.connect() as connection:
                connection.execute(select(1)).scalar_one()
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError(
                "Control-plane persistence is unavailable"
            ) from error

    def schema_is_current(self) -> bool:
        """Return readiness without creating or mutating database objects."""
        try:
            with self._engine.connect() as connection:
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
        except SQLAlchemyError:
            return False
        return revision == _SCHEMA_REVISION

    def put_dataset(self, record: DatasetRecord) -> DatasetRecord:
        document = _model_text(record.dataset)
        self._require_document_size(document)
        values = {
            "name": record.dataset.name,
            "revision": record.dataset.revision,
            "digest": record.dataset.digest,
            "case_count": len(record.dataset.cases),
            "document": document,
            "created_at": record.created_at,
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(insert(datasets_table).values(**values))
            return record
        except IntegrityError:
            try:
                existing = self.get_dataset(
                    record.dataset.name, record.dataset.revision
                )
            except RecordNotFoundError as error:
                raise ImmutableRecordConflictError(
                    "Dataset revision conflicts with existing metadata"
                ) from error
            if _model_text(existing.dataset) != document:
                raise ImmutableRecordConflictError(
                    "Dataset revision already contains different evidence"
                ) from None
            return existing
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError(
                "Could not store dataset revision"
            ) from error

    def get_dataset(self, name: str, revision: int) -> DatasetRecord:
        try:
            with self._engine.connect() as connection:
                row = (
                    connection.execute(
                        select(datasets_table).where(
                            datasets_table.c.name == name,
                            datasets_table.c.revision == revision,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError(
                "Could not load dataset revision"
            ) from error
        if row is None:
            raise RecordNotFoundError("Dataset revision was not found")
        return self._dataset_record(row)

    def list_datasets(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        name: str | None = None,
    ) -> CursorPage[DatasetListRecord]:
        page_limit = _limit(limit)
        filters: dict[str, JsonValue] = {"name": name}
        statement = select(
            datasets_table.c.name,
            datasets_table.c.revision,
            datasets_table.c.digest,
            datasets_table.c.case_count,
            datasets_table.c.created_at,
        )
        if name is not None:
            statement = statement.where(datasets_table.c.name == name)
        if cursor is not None:
            key = _decode_cursor(cursor, stream="datasets", filters=filters)
            if (
                len(key) != 3
                or not isinstance(key[1], str)
                or isinstance(key[2], bool)
                or not isinstance(key[2], int)
            ):
                raise InvalidCursorError("Pagination cursor is invalid")
            created_at = _decoded_cursor_time(key[0])
            statement = statement.where(
                (datasets_table.c.created_at > created_at)
                | and_(
                    datasets_table.c.created_at == created_at,
                    datasets_table.c.name > key[1],
                )
                | and_(
                    datasets_table.c.created_at == created_at,
                    datasets_table.c.name == key[1],
                    datasets_table.c.revision > key[2],
                )
            )
        statement = statement.order_by(
            datasets_table.c.created_at,
            datasets_table.c.name,
            datasets_table.c.revision,
        ).limit(page_limit + 1)
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(statement).mappings().all()
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError(
                "Could not list dataset revisions"
            ) from error
        records = tuple(self._dataset_list_record(row) for row in rows[:page_limit])
        next_cursor = None
        if len(rows) > page_limit:
            last = records[-1]
            next_cursor = _encode_cursor(
                stream="datasets",
                filters=filters,
                key=[
                    _cursor_time(records[-1].created_at),
                    last.name,
                    last.revision,
                ],
            )
        return CursorPage(items=records, next_cursor=next_cursor)

    def begin_job(
        self,
        record: JobRecord,
        payload: JobPayload,
    ) -> tuple[JobRecord, bool]:
        """Atomically insert one queued job and its canonical private payload."""
        if record.status is not JobStatus.QUEUED or record.attempt_count != 0:
            raise ValueError("new jobs must start as an unattempted queued job")
        if payload.kind is not record.kind:
            raise ValueError("job payload kind does not match the job")
        document = _model_text(payload)
        self._require_payload_size(document)
        values = {
            "job_id": record.job_id,
            "kind": record.kind.value,
            "status": record.status.value,
            "idempotency_key": record.idempotency_key,
            "request_digest": record.request_digest,
            "resource_id": record.resource_id,
            "attempt_count": record.attempt_count,
            "max_attempts": record.max_attempts,
            "available_at": record.available_at,
            "error_code": record.error_code,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "version": 0,
        }
        payload_values = {
            "job_id": record.job_id,
            "payload_digest": payload.payload_digest,
            "document": document,
            "created_at": record.created_at,
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(insert(jobs_table).values(**values))
                connection.execute(insert(job_payloads_table).values(**payload_values))
            return record, True
        except IntegrityError:
            try:
                existing = self.get_job_by_idempotency(
                    record.kind, record.idempotency_key
                )
            except RecordNotFoundError as error:
                try:
                    self.get_job_by_resource(record.kind, record.resource_id)
                except RecordNotFoundError:
                    raise ImmutableRecordConflictError(
                        "Job identity conflicts with existing metadata"
                    ) from error
                raise ResourceAlreadySubmittedError(
                    "Resource ID was already submitted"
                ) from error
            if existing.request_digest != record.request_digest:
                raise IdempotencyConflictError(
                    "Idempotency key was already used for a different request"
                ) from None
            return existing, False
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError("Could not submit job") from error

    def get_job(self, job_id: str) -> JobRecord:
        return self._get_job(jobs_table.c.job_id == job_id)

    def get_job_by_idempotency(self, kind: JobKind, idempotency_key: str) -> JobRecord:
        job = self._get_job(
            and_(
                jobs_table.c.kind == kind.value,
                jobs_table.c.idempotency_key == idempotency_key,
            )
        )
        self._validate_existing_job_payload(job)
        return job

    def get_job_by_resource(self, kind: JobKind, resource_id: str) -> JobRecord:
        return self._get_job(
            and_(
                jobs_table.c.kind == kind.value,
                jobs_table.c.resource_id == resource_id,
            )
        )

    def _get_job(self, condition: Any) -> JobRecord:
        try:
            with self._engine.connect() as connection:
                row = (
                    connection.execute(select(jobs_table).where(condition))
                    .mappings()
                    .one_or_none()
                )
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError("Could not load job") from error
        if row is None:
            raise RecordNotFoundError("Job was not found")
        return self._job_record(row)

    def list_jobs(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        kind: JobKind | None = None,
        status: JobStatus | None = None,
    ) -> CursorPage[JobRecord]:
        page_limit = _limit(limit)
        filters: dict[str, JsonValue] = {
            "kind": None if kind is None else kind.value,
            "status": None if status is None else status.value,
        }
        statement = select(jobs_table)
        if kind is not None:
            statement = statement.where(jobs_table.c.kind == kind.value)
        if status is not None:
            statement = statement.where(jobs_table.c.status == status.value)
        if cursor is not None:
            key = _decode_cursor(cursor, stream="jobs", filters=filters)
            if len(key) != 2 or not isinstance(key[1], str):
                raise InvalidCursorError("Pagination cursor is invalid")
            created_at = _decoded_cursor_time(key[0])
            statement = statement.where(
                (jobs_table.c.created_at > created_at)
                | and_(
                    jobs_table.c.created_at == created_at,
                    jobs_table.c.job_id > key[1],
                )
            )
        statement = statement.order_by(
            jobs_table.c.created_at, jobs_table.c.job_id
        ).limit(page_limit + 1)
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(statement).mappings().all()
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError("Could not list jobs") from error
        records = tuple(self._job_record(row) for row in rows[:page_limit])
        next_cursor = None
        if len(rows) > page_limit:
            next_cursor = _encode_cursor(
                stream="jobs",
                filters=filters,
                key=[_cursor_time(records[-1].created_at), records[-1].job_id],
            )
        return CursorPage(items=records, next_cursor=next_cursor)

    def claim_next_job(
        self,
        *,
        worker_id: WorkerId,
        lease_token: LeaseToken,
        lease_seconds: int,
    ) -> ClaimedJob | None:
        worker = _worker_id_value(worker_id)
        token = _lease_token_value(lease_token)
        duration = _bounded_int(
            lease_seconds,
            lower=5,
            upper=3_600,
            name="lease duration",
        )
        self._require_postgresql_coordination()
        try:
            with self._engine.begin() as connection:
                selection_time = self._database_now(connection)
                row = (
                    connection.execute(
                        select(jobs_table)
                        .where(
                            jobs_table.c.status == JobStatus.QUEUED.value,
                            jobs_table.c.available_at <= selection_time,
                        )
                        .order_by(
                            jobs_table.c.available_at,
                            jobs_table.c.created_at,
                            jobs_table.c.job_id,
                        )
                        .with_for_update(skip_locked=True, of=jobs_table)
                        .limit(1)
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    return None
                current = self._job_record(row)
                payload = self._load_job_payload(
                    connection,
                    current,
                    required=True,
                    lock=True,
                )
                if payload is None:  # pragma: no cover - required=True is exhaustive
                    raise CorruptRecordError("Queued job has no worker payload")
                lease_started_at = self._database_now(connection)
                transition_at = max(lease_started_at, current.updated_at)
                try:
                    running = current.transition_to(
                        JobStatus.RUNNING,
                        at=transition_at,
                    )
                except ValueError as error:
                    raise IllegalJobTransitionError("Job cannot be claimed") from error
                lease_expires_at = lease_started_at + timedelta(seconds=duration)
                self._update_job(connection, row=row, record=running)
                connection.execute(
                    insert(job_attempts_table).values(
                        job_id=running.job_id,
                        attempt_number=running.attempt_count,
                        status=JobAttemptStatus.RUNNING.value,
                        worker_id=worker,
                        lease_token=token,
                        error_code=None,
                        started_at=lease_started_at,
                        heartbeat_at=lease_started_at,
                        lease_expires_at=lease_expires_at,
                        finished_at=None,
                    )
                )
                attempt = JobAttemptRecord(
                    job_id=running.job_id,
                    attempt_number=running.attempt_count,
                    status=JobAttemptStatus.RUNNING,
                    started_at=lease_started_at,
                    heartbeat_at=lease_started_at,
                    lease_expires_at=lease_expires_at,
                )
                return ClaimedJob(
                    job=running,
                    payload=payload,
                    attempt=attempt,
                    lease_token=token,
                )
        except ControlPlaneRepositoryError:
            raise
        except IntegrityError as error:
            raise ConcurrentTransitionError(
                "Job claim conflicted with another writer"
            ) from error
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError("Could not claim queued job") from error

    def heartbeat_job(
        self,
        job_id: str,
        attempt_number: int,
        lease_token: LeaseToken,
        *,
        lease_seconds: int,
    ) -> JobRecord:
        number = _bounded_int(
            attempt_number,
            lower=1,
            upper=10,
            name="attempt number",
        )
        token = _lease_token_value(lease_token)
        duration = _bounded_int(
            lease_seconds,
            lower=5,
            upper=3_600,
            name="lease duration",
        )
        try:
            with self._engine.begin() as connection:
                _, current, _, locked_at = self._lock_active_lease(
                    connection,
                    job_id=job_id,
                    attempt_number=number,
                    lease_token=token,
                )
                changed = connection.execute(
                    update(job_attempts_table)
                    .where(
                        job_attempts_table.c.job_id == job_id,
                        job_attempts_table.c.attempt_number == number,
                        job_attempts_table.c.status == JobAttemptStatus.RUNNING.value,
                        job_attempts_table.c.lease_token == token,
                        job_attempts_table.c.lease_expires_at
                        > self._database_time_expression(connection),
                    )
                    .values(
                        heartbeat_at=locked_at,
                        lease_expires_at=locked_at + timedelta(seconds=duration),
                    )
                )
                if changed.rowcount != 1:
                    raise LeaseLostError("Worker lease is no longer active")
                return current
        except ControlPlaneRepositoryError:
            raise
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError(
                "Could not heartbeat worker lease"
            ) from error

    def retry_job(
        self,
        job_id: str,
        attempt_number: int,
        lease_token: LeaseToken,
        *,
        delay_seconds: int,
        error_code: str,
    ) -> JobRecord:
        number = _bounded_int(
            attempt_number,
            lower=1,
            upper=10,
            name="attempt number",
        )
        token = _lease_token_value(lease_token)
        delay = _bounded_int(
            delay_seconds,
            lower=1,
            upper=3_600,
            name="retry delay",
        )
        safe_error = _safe_code(error_code)
        try:
            with self._engine.begin() as connection:
                row, current, _, locked_at = self._lock_active_lease(
                    connection,
                    job_id=job_id,
                    attempt_number=number,
                    lease_token=token,
                )
                if current.status is JobStatus.CANCEL_REQUESTED:
                    return self._cancel_active_attempt(
                        connection,
                        row=row,
                        current=current,
                        attempt_number=number,
                        lease_token=token,
                        now=locked_at,
                    )
                transition_at = max(locked_at, current.updated_at)
                try:
                    queued = current.transition_to(
                        JobStatus.QUEUED,
                        at=transition_at,
                        available_at=transition_at + timedelta(seconds=delay),
                    )
                except ValueError as error:
                    raise IllegalJobTransitionError(
                        "Job cannot be scheduled for another attempt"
                    ) from error
                self._finish_attempt(
                    connection,
                    job_id=job_id,
                    attempt_number=number,
                    lease_token=token,
                    status=JobAttemptStatus.RETRY_SCHEDULED,
                    error_code=safe_error,
                    finished_at=transition_at,
                )
                self._update_job(connection, row=row, record=queued)
                return queued
        except ControlPlaneRepositoryError:
            raise
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError("Could not schedule job retry") from error

    def fail_job(
        self,
        job_id: str,
        attempt_number: int,
        lease_token: LeaseToken,
        *,
        error_code: str,
    ) -> JobRecord:
        number = _bounded_int(
            attempt_number,
            lower=1,
            upper=10,
            name="attempt number",
        )
        token = _lease_token_value(lease_token)
        safe_error = _safe_code(error_code)
        try:
            with self._engine.begin() as connection:
                row, current, _, locked_at = self._lock_active_lease(
                    connection,
                    job_id=job_id,
                    attempt_number=number,
                    lease_token=token,
                )
                if current.status is JobStatus.CANCEL_REQUESTED:
                    return self._cancel_active_attempt(
                        connection,
                        row=row,
                        current=current,
                        attempt_number=number,
                        lease_token=token,
                        now=locked_at,
                    )
                transition_at = max(locked_at, current.updated_at)
                try:
                    failed = current.transition_to(
                        JobStatus.FAILED,
                        at=transition_at,
                        error_code=safe_error,
                    )
                except ValueError as error:
                    raise IllegalJobTransitionError("Job cannot fail") from error
                self._finish_attempt(
                    connection,
                    job_id=job_id,
                    attempt_number=number,
                    lease_token=token,
                    status=JobAttemptStatus.FAILED,
                    error_code=safe_error,
                    finished_at=transition_at,
                )
                self._update_job(connection, row=row, record=failed)
                return failed
        except ControlPlaneRepositoryError:
            raise
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError("Could not fail job") from error

    def reap_expired_jobs(
        self,
        *,
        limit: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
    ) -> tuple[JobRecord, ...]:
        batch_limit = _bounded_int(limit, lower=1, upper=100, name="reaper limit")
        retry_base = _bounded_int(
            retry_base_seconds,
            lower=1,
            upper=300,
            name="retry base",
        )
        retry_max = _bounded_int(
            retry_max_seconds,
            lower=1,
            upper=3_600,
            name="retry maximum",
        )
        if retry_max < retry_base:
            raise ValueError("retry maximum cannot be below retry base")
        self._require_postgresql_coordination()
        try:
            with self._engine.begin() as connection:
                selection_time = self._database_now(connection)
                candidates = connection.execute(
                    select(
                        jobs_table.c.job_id,
                        job_attempts_table.c.attempt_number,
                    )
                    .join(
                        job_attempts_table,
                        and_(
                            job_attempts_table.c.job_id == jobs_table.c.job_id,
                            job_attempts_table.c.attempt_number
                            == jobs_table.c.attempt_count,
                        ),
                    )
                    .where(
                        jobs_table.c.status.in_(
                            (
                                JobStatus.RUNNING.value,
                                JobStatus.CANCEL_REQUESTED.value,
                            )
                        ),
                        job_attempts_table.c.status == JobAttemptStatus.RUNNING.value,
                        job_attempts_table.c.lease_expires_at <= selection_time,
                    )
                    .order_by(
                        job_attempts_table.c.lease_expires_at,
                        jobs_table.c.job_id,
                    )
                    .with_for_update(
                        skip_locked=True,
                        of=(jobs_table, job_attempts_table),
                    )
                    .limit(batch_limit)
                ).all()
                recovered: list[JobRecord] = []
                for job_id, attempt_number in candidates:
                    job_row = self._locked_job_row(connection, job_id)
                    attempt_row = self._locked_attempt_row(
                        connection,
                        job_id=job_id,
                        attempt_number=attempt_number,
                    )
                    if job_row is None or attempt_row is None:
                        raise CorruptRecordError("Expired worker state is incomplete")
                    current = self._job_record(job_row)
                    transition_time = self._database_now(connection)
                    transition_at = max(transition_time, current.updated_at)
                    token = attempt_row["lease_token"]
                    if not isinstance(token, str):
                        raise CorruptRecordError("Stored worker lease is invalid")
                    if current.status is JobStatus.CANCEL_REQUESTED:
                        recovered.append(
                            self._cancel_active_attempt(
                                connection,
                                row=job_row,
                                current=current,
                                attempt_number=attempt_number,
                                lease_token=token,
                                now=transition_at,
                                allow_expired_lease=True,
                            )
                        )
                        continue
                    self._finish_attempt(
                        connection,
                        job_id=job_id,
                        attempt_number=attempt_number,
                        lease_token=token,
                        status=JobAttemptStatus.LEASE_EXPIRED,
                        error_code="lease_expired",
                        finished_at=transition_at,
                        allow_expired_lease=True,
                    )
                    if current.attempt_count >= current.max_attempts:
                        next_record = current.transition_to(
                            JobStatus.FAILED,
                            at=transition_at,
                            error_code="lease_expired",
                        )
                    else:
                        delay = min(
                            retry_base * 2 ** (current.attempt_count - 1),
                            retry_max,
                        )
                        next_record = current.transition_to(
                            JobStatus.QUEUED,
                            at=transition_at,
                            available_at=transition_at + timedelta(seconds=delay),
                        )
                    self._update_job(
                        connection,
                        row=job_row,
                        record=next_record,
                    )
                    recovered.append(next_record)
                return tuple(recovered)
        except ControlPlaneRepositoryError:
            raise
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError(
                "Could not recover expired jobs"
            ) from error

    def cancel_job(self, job_id: str) -> JobRecord:
        try:
            with self._engine.begin() as connection:
                row = self._locked_job_row(connection, job_id)
                if row is None:
                    raise RecordNotFoundError("Job was not found")
                current = self._job_record(row)
                if current.status is JobStatus.CANCELED:
                    return current
                if current.status is JobStatus.CANCEL_REQUESTED:
                    return current
                if current.status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
                    raise IllegalJobTransitionError("Terminal job cannot be canceled")
                if current.status is JobStatus.RUNNING:
                    attempt = self._locked_attempt_row(
                        connection,
                        job_id=job_id,
                        attempt_number=current.attempt_count,
                    )
                    if (
                        attempt is None
                        or attempt["status"] != JobAttemptStatus.RUNNING.value
                    ):
                        raise CorruptRecordError("Running job has no active attempt")
                transition_time = self._database_now(connection)
                transition_at = max(transition_time, current.updated_at)
                canceled = current.request_cancellation(at=transition_at)
                self._update_job(connection, row=row, record=canceled)
                return canceled
        except ControlPlaneRepositoryError:
            raise
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError("Could not cancel job") from error

    def acknowledge_cancellation(
        self,
        job_id: str,
        attempt_number: int,
        lease_token: LeaseToken,
    ) -> JobRecord:
        number = _bounded_int(
            attempt_number,
            lower=1,
            upper=10,
            name="attempt number",
        )
        token = _lease_token_value(lease_token)
        try:
            with self._engine.begin() as connection:
                row = self._locked_job_row(connection, job_id)
                if row is None:
                    raise LeaseLostError("Worker lease is no longer active")
                current = self._job_record(row)
                if current.status is JobStatus.CANCELED:
                    self._require_terminal_attempt(
                        connection,
                        job_id=job_id,
                        attempt_number=number,
                        lease_token=token,
                        status=JobAttemptStatus.CANCELED,
                    )
                    return current
                row, current, _, locked_at = self._lock_active_lease(
                    connection,
                    job_id=job_id,
                    attempt_number=number,
                    lease_token=token,
                    locked_job=row,
                )
                if current.status is not JobStatus.CANCEL_REQUESTED:
                    raise IllegalJobTransitionError("Job has no cancellation request")
                return self._cancel_active_attempt(
                    connection,
                    row=row,
                    current=current,
                    attempt_number=number,
                    lease_token=token,
                    now=locked_at,
                )
        except ControlPlaneRepositoryError:
            raise
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError(
                "Could not acknowledge job cancellation"
            ) from error

    def list_job_attempts(self, job_id: str) -> tuple[JobAttemptRecord, ...]:
        statement = (
            select(
                jobs_table,
                job_attempts_table.c.job_id.label("_attempt_job_id"),
                job_attempts_table.c.attempt_number.label("_attempt_attempt_number"),
                job_attempts_table.c.status.label("_attempt_status"),
                job_attempts_table.c.worker_id.label("_attempt_worker_id"),
                job_attempts_table.c.lease_token.label("_attempt_lease_token"),
                job_attempts_table.c.error_code.label("_attempt_error_code"),
                job_attempts_table.c.started_at.label("_attempt_started_at"),
                job_attempts_table.c.heartbeat_at.label("_attempt_heartbeat_at"),
                job_attempts_table.c.lease_expires_at.label(
                    "_attempt_lease_expires_at"
                ),
                job_attempts_table.c.finished_at.label("_attempt_finished_at"),
            )
            .select_from(
                jobs_table.outerjoin(
                    job_attempts_table,
                    job_attempts_table.c.job_id == jobs_table.c.job_id,
                )
            )
            .where(jobs_table.c.job_id == job_id)
            .order_by(job_attempts_table.c.attempt_number)
        )
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(statement).mappings().all()
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError("Could not list job attempts") from error
        if not rows:
            raise RecordNotFoundError("Job was not found")
        job = self._job_record(rows[0])
        records = tuple(
            self._attempt_record(
                {
                    "job_id": row["_attempt_job_id"],
                    "attempt_number": row["_attempt_attempt_number"],
                    "status": row["_attempt_status"],
                    "worker_id": row["_attempt_worker_id"],
                    "lease_token": row["_attempt_lease_token"],
                    "error_code": row["_attempt_error_code"],
                    "started_at": row["_attempt_started_at"],
                    "heartbeat_at": row["_attempt_heartbeat_at"],
                    "lease_expires_at": row["_attempt_lease_expires_at"],
                    "finished_at": row["_attempt_finished_at"],
                }
            )
            for row in rows
            if row["_attempt_job_id"] is not None
        )
        if tuple(item.attempt_number for item in records) != tuple(
            range(1, job.attempt_count + 1)
        ):
            raise CorruptRecordError("Stored job attempt history is incomplete")
        return records

    def _validate_existing_job_payload(
        self,
        job: JobRecord,
    ) -> None:
        terminal = job.status in (
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELED,
        )
        try:
            with self._engine.connect() as connection:
                self._load_job_payload(
                    connection,
                    job,
                    required=not terminal,
                )
        except ControlPlaneRepositoryError:
            raise
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError(
                "Could not validate job payload"
            ) from error

    def _load_job_payload(
        self,
        connection: Connection,
        job: JobRecord,
        *,
        required: bool,
        lock: bool = False,
    ) -> JobPayload | None:
        statement = select(job_payloads_table).where(
            job_payloads_table.c.job_id == job.job_id
        )
        if lock:
            statement = statement.with_for_update(of=job_payloads_table)
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            if not required and self._is_migrated_terminal_job(connection, job):
                return None
            raise CorruptRecordError("Stored job has no worker payload")
        try:
            document = row["document"]
            if not isinstance(document, str):
                raise TypeError("worker payload must be text")
            self._require_payload_size(document)
            value = parse_json(document)
            if canonical_json_bytes(value).decode("utf-8") != document:
                raise ValueError("worker payload is not canonical")
            payload = _JOB_PAYLOAD_ADAPTER.validate_python(value)
            if row["payload_digest"] != payload.payload_digest:
                raise ValueError("worker payload digest does not match")
            if payload.kind is not job.kind:
                raise ValueError("worker payload kind does not match")
            if _aware(row["created_at"]) != job.created_at:
                raise ValueError("worker payload timestamp does not match")
            return payload
        except (
            CanonicalJsonError,
            KeyError,
            PayloadTooLargeError,
            TypeError,
            UnicodeError,
            ValidationError,
            ValueError,
        ) as error:
            raise CorruptRecordError("Stored worker payload is invalid") from error

    def _is_migrated_terminal_job(
        self,
        connection: Connection,
        job: JobRecord,
    ) -> bool:
        if (
            job.status not in (JobStatus.SUCCEEDED, JobStatus.FAILED)
            or job.attempt_count != 1
            or job.max_attempts != 3
            or job.available_at != job.created_at
        ):
            return False
        row = (
            connection.execute(
                select(job_attempts_table).where(
                    job_attempts_table.c.job_id == job.job_id,
                    job_attempts_table.c.attempt_number == 1,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        attempt = self._attempt_record(row)
        lease_delta = attempt.lease_expires_at - job.updated_at
        return (
            row["worker_id"] == _MIGRATION_WORKER_ID
            and row["lease_token"] == _MIGRATION_LEASE_TOKEN
            and attempt.status.value == job.status.value
            and attempt.error_code == job.error_code
            and attempt.started_at == job.created_at
            and attempt.heartbeat_at == job.updated_at
            and attempt.finished_at == job.updated_at
            and timedelta(0) < lease_delta <= timedelta(seconds=1)
        )

    def _require_postgresql_coordination(self) -> None:
        if self._engine.dialect.name != "postgresql":
            raise ControlPlaneRepositoryError(
                "Leased worker coordination requires PostgreSQL"
            )

    @staticmethod
    def _database_time_expression(connection: Connection) -> Any:
        return (
            func.statement_timestamp()
            if connection.dialect.name == "postgresql"
            else func.current_timestamp()
        )

    @classmethod
    def _database_now(cls, connection: Connection) -> datetime:
        return _aware(
            connection.execute(
                select(cls._database_time_expression(connection))
            ).scalar_one()
        )

    @staticmethod
    def _locked_job_row(
        connection: Connection,
        job_id: str,
    ) -> RowMapping | None:
        return (
            connection.execute(
                select(jobs_table)
                .where(jobs_table.c.job_id == job_id)
                .with_for_update(of=jobs_table)
            )
            .mappings()
            .one_or_none()
        )

    @staticmethod
    def _locked_attempt_row(
        connection: Connection,
        *,
        job_id: str,
        attempt_number: int,
    ) -> RowMapping | None:
        return (
            connection.execute(
                select(job_attempts_table)
                .where(
                    job_attempts_table.c.job_id == job_id,
                    job_attempts_table.c.attempt_number == attempt_number,
                )
                .with_for_update(of=job_attempts_table)
            )
            .mappings()
            .one_or_none()
        )

    def _lock_active_lease(
        self,
        connection: Connection,
        *,
        job_id: str,
        attempt_number: int,
        lease_token: str,
        locked_job: RowMapping | None = None,
    ) -> tuple[RowMapping, JobRecord, RowMapping, datetime]:
        job_row = (
            self._locked_job_row(connection, job_id)
            if locked_job is None
            else locked_job
        )
        if job_row is None:
            raise LeaseLostError("Worker lease is no longer active")
        current = self._job_record(job_row)
        attempt_row = self._locked_attempt_row(
            connection,
            job_id=job_id,
            attempt_number=attempt_number,
        )
        if attempt_row is None:
            raise LeaseLostError("Worker lease is no longer active")
        self._attempt_record(attempt_row)
        locked_at = self._database_now(connection)
        if (
            current.status not in (JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED)
            or current.attempt_count != attempt_number
            or attempt_row["status"] != JobAttemptStatus.RUNNING.value
            or attempt_row["lease_token"] != lease_token
            or _aware(attempt_row["lease_expires_at"]) <= locked_at
        ):
            raise LeaseLostError("Worker lease is no longer active")
        return job_row, current, attempt_row, locked_at

    @staticmethod
    def _update_job(
        connection: Connection,
        *,
        row: RowMapping,
        record: JobRecord,
    ) -> None:
        version = row["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise CorruptRecordError("Stored job version is invalid")
        changed = connection.execute(
            update(jobs_table)
            .where(
                jobs_table.c.job_id == record.job_id,
                jobs_table.c.version == version,
            )
            .values(
                status=record.status.value,
                attempt_count=record.attempt_count,
                max_attempts=record.max_attempts,
                available_at=record.available_at,
                error_code=record.error_code,
                updated_at=record.updated_at,
                version=version + 1,
            )
        )
        if changed.rowcount != 1:
            raise ConcurrentTransitionError("Job changed during worker transition")

    @staticmethod
    def _finish_attempt(
        connection: Connection,
        *,
        job_id: str,
        attempt_number: int,
        lease_token: str,
        status: JobAttemptStatus,
        error_code: str | None,
        finished_at: datetime,
        allow_expired_lease: bool = False,
    ) -> None:
        conditions = [
            job_attempts_table.c.job_id == job_id,
            job_attempts_table.c.attempt_number == attempt_number,
            job_attempts_table.c.status == JobAttemptStatus.RUNNING.value,
            job_attempts_table.c.lease_token == lease_token,
        ]
        if not allow_expired_lease:
            conditions.append(
                job_attempts_table.c.lease_expires_at
                > SqlAlchemyControlPlaneRepository._database_time_expression(connection)
            )
        changed = connection.execute(
            update(job_attempts_table)
            .where(*conditions)
            .values(
                status=status.value,
                error_code=error_code,
                finished_at=finished_at,
            )
        )
        if changed.rowcount != 1:
            raise LeaseLostError("Worker lease is no longer active")

    def _cancel_active_attempt(
        self,
        connection: Connection,
        *,
        row: RowMapping,
        current: JobRecord,
        attempt_number: int,
        lease_token: str,
        now: datetime,
        allow_expired_lease: bool = False,
    ) -> JobRecord:
        transition_at = max(now, current.updated_at)
        try:
            canceled = current.transition_to(
                JobStatus.CANCELED,
                at=transition_at,
            )
        except ValueError as error:
            raise IllegalJobTransitionError("Job cannot be canceled") from error
        self._finish_attempt(
            connection,
            job_id=current.job_id,
            attempt_number=attempt_number,
            lease_token=lease_token,
            status=JobAttemptStatus.CANCELED,
            error_code=None,
            finished_at=transition_at,
            allow_expired_lease=allow_expired_lease,
        )
        self._update_job(connection, row=row, record=canceled)
        return canceled

    def _require_terminal_attempt(
        self,
        connection: Connection,
        *,
        job_id: str,
        attempt_number: int,
        lease_token: str,
        status: JobAttemptStatus,
    ) -> JobAttemptRecord:
        row = self._locked_attempt_row(
            connection,
            job_id=job_id,
            attempt_number=attempt_number,
        )
        if (
            row is None
            or row["lease_token"] != lease_token
            or row["status"] != status.value
        ):
            raise LeaseLostError("Worker lease is no longer active")
        return self._attempt_record(row)

    def complete_run(
        self,
        job_id: str,
        record: RunRecord,
        *,
        attempt_number: int,
        lease_token: LeaseToken,
    ) -> JobRecord:
        """Publish immutable run evidence through one unexpired fenced attempt."""
        number = _bounded_int(
            attempt_number,
            lower=1,
            upper=10,
            name="attempt number",
        )
        token = _lease_token_value(lease_token)
        document = _model_text(record.result)
        self._require_document_size(document)
        values = {
            "run_id": record.run_id,
            "result_digest": record.result.result_digest,
            "dataset_name": record.result.dataset.name,
            "dataset_revision": record.result.dataset.revision,
            "status": record.result.status.value,
            "execution_mode": record.result.execution_mode.value,
            "document": document,
            "created_at": record.created_at,
        }
        try:
            with self._engine.begin() as connection:
                row = self._locked_job_row(connection, job_id)
                if row is None:
                    raise LeaseLostError("Worker lease is no longer active")
                current = self._job_record(row)
                if current.status not in (
                    JobStatus.RUNNING,
                    JobStatus.CANCEL_REQUESTED,
                    JobStatus.SUCCEEDED,
                ):
                    raise LeaseLostError("Worker lease is no longer active")
                self._require_completion_identity(
                    current,
                    expected_kind=JobKind.RUN,
                    resource_id=record.run_id,
                )
                stored = (
                    connection.execute(
                        select(runs_table).where(runs_table.c.run_id == record.run_id)
                    )
                    .mappings()
                    .one_or_none()
                )
                if current.status is JobStatus.SUCCEEDED:
                    self._require_terminal_attempt(
                        connection,
                        job_id=job_id,
                        attempt_number=number,
                        lease_token=token,
                        status=JobAttemptStatus.SUCCEEDED,
                    )
                    self._require_identical_run(stored, document)
                    return current
                row, current, _, locked_at = self._lock_active_lease(
                    connection,
                    job_id=job_id,
                    attempt_number=number,
                    lease_token=token,
                    locked_job=row,
                )
                if current.status is JobStatus.CANCEL_REQUESTED:
                    return self._cancel_active_attempt(
                        connection,
                        row=row,
                        current=current,
                        attempt_number=number,
                        lease_token=token,
                        now=locked_at,
                    )
                if stored is None:
                    connection.execute(insert(runs_table).values(**values))
                else:
                    self._require_identical_run(stored, document)
                transition_at = max(locked_at, current.updated_at)
                succeeded = current.transition_to(
                    JobStatus.SUCCEEDED,
                    at=transition_at,
                )
                self._finish_attempt(
                    connection,
                    job_id=job_id,
                    attempt_number=number,
                    lease_token=token,
                    status=JobAttemptStatus.SUCCEEDED,
                    error_code=None,
                    finished_at=transition_at,
                )
                self._update_job(
                    connection,
                    row=row,
                    record=succeeded,
                )
                return succeeded
        except ControlPlaneRepositoryError:
            raise
        except IntegrityError as error:
            raise ImmutableRecordConflictError(
                "Run evidence conflicts with stored metadata"
            ) from error
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError("Could not complete run job") from error

    def complete_release_decision(
        self,
        job_id: str,
        record: ReleaseDecisionRecord,
        *,
        attempt_number: int,
        lease_token: LeaseToken,
    ) -> JobRecord:
        """Publish immutable decision evidence through one fenced attempt."""
        number = _bounded_int(
            attempt_number,
            lower=1,
            upper=10,
            name="attempt number",
        )
        token = _lease_token_value(lease_token)
        document = _model_text(record.decision)
        self._require_document_size(document)
        values = {
            "decision_id": record.decision_id,
            "decision_digest": record.decision.decision_digest,
            "baseline_run_id": record.decision.baseline_run_id,
            "candidate_run_id": record.decision.candidate_run_id,
            "status": record.decision.status.value,
            "document": document,
            "created_at": record.created_at,
        }
        try:
            with self._engine.begin() as connection:
                row = self._locked_job_row(connection, job_id)
                if row is None:
                    raise LeaseLostError("Worker lease is no longer active")
                current = self._job_record(row)
                if current.status not in (
                    JobStatus.RUNNING,
                    JobStatus.CANCEL_REQUESTED,
                    JobStatus.SUCCEEDED,
                ):
                    raise LeaseLostError("Worker lease is no longer active")
                self._require_completion_identity(
                    current,
                    expected_kind=JobKind.COMPARISON,
                    resource_id=record.decision_id,
                )
                stored = (
                    connection.execute(
                        select(release_decisions_table).where(
                            release_decisions_table.c.decision_id == record.decision_id
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if current.status is JobStatus.SUCCEEDED:
                    self._require_terminal_attempt(
                        connection,
                        job_id=job_id,
                        attempt_number=number,
                        lease_token=token,
                        status=JobAttemptStatus.SUCCEEDED,
                    )
                    self._require_identical_decision(stored, document)
                    return current
                row, current, _, locked_at = self._lock_active_lease(
                    connection,
                    job_id=job_id,
                    attempt_number=number,
                    lease_token=token,
                    locked_job=row,
                )
                if current.status is JobStatus.CANCEL_REQUESTED:
                    return self._cancel_active_attempt(
                        connection,
                        row=row,
                        current=current,
                        attempt_number=number,
                        lease_token=token,
                        now=locked_at,
                    )
                if stored is None:
                    connection.execute(insert(release_decisions_table).values(**values))
                else:
                    self._require_identical_decision(stored, document)
                transition_at = max(locked_at, current.updated_at)
                succeeded = current.transition_to(
                    JobStatus.SUCCEEDED,
                    at=transition_at,
                )
                self._finish_attempt(
                    connection,
                    job_id=job_id,
                    attempt_number=number,
                    lease_token=token,
                    status=JobAttemptStatus.SUCCEEDED,
                    error_code=None,
                    finished_at=transition_at,
                )
                self._update_job(
                    connection,
                    row=row,
                    record=succeeded,
                )
                return succeeded
        except ControlPlaneRepositoryError:
            raise
        except IntegrityError as error:
            raise ConcurrentTransitionError(
                "Decision completion conflicted with another writer"
            ) from error
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError(
                "Could not complete comparison job"
            ) from error

    @staticmethod
    def _require_completion_identity(
        current: JobRecord,
        *,
        expected_kind: JobKind,
        resource_id: str,
    ) -> None:
        if current.kind is not expected_kind or current.resource_id != resource_id:
            raise IllegalJobTransitionError("Job does not own the completed resource")
        if current.status not in (
            JobStatus.RUNNING,
            JobStatus.CANCEL_REQUESTED,
            JobStatus.SUCCEEDED,
        ):
            raise IllegalJobTransitionError(
                "Only running jobs can publish completed evidence"
            )

    def _require_identical_run(
        self, row: RowMapping | None, expected_document: str
    ) -> None:
        if row is None:
            raise CorruptRecordError("Succeeded run job has no stored evidence")
        stored = self._run_record(row)
        if _model_text(stored.result) != expected_document:
            raise ImmutableRecordConflictError(
                "Run ID already contains different evidence"
            )

    def _require_identical_decision(
        self, row: RowMapping | None, expected_document: str
    ) -> None:
        if row is None:
            raise CorruptRecordError("Succeeded comparison job has no stored evidence")
        stored = self._release_decision_record(row)
        if _model_text(stored.decision) != expected_document:
            raise ImmutableRecordConflictError(
                "Decision ID already contains different evidence"
            )

    def put_run(self, record: RunRecord) -> RunRecord:
        document = _model_text(record.result)
        self._require_document_size(document)
        values = {
            "run_id": record.run_id,
            "result_digest": record.result.result_digest,
            "dataset_name": record.result.dataset.name,
            "dataset_revision": record.result.dataset.revision,
            "status": record.result.status.value,
            "execution_mode": record.result.execution_mode.value,
            "document": document,
            "created_at": record.created_at,
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(insert(runs_table).values(**values))
            return record
        except IntegrityError:
            try:
                existing = self.get_run(record.run_id)
            except RecordNotFoundError as error:
                raise ImmutableRecordConflictError(
                    "Run conflicts with existing metadata"
                ) from error
            if _model_text(existing.result) != document:
                raise ImmutableRecordConflictError(
                    "Run ID already contains different evidence"
                ) from None
            return existing
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError("Could not store run evidence") from error

    def get_run(self, run_id: str) -> RunRecord:
        try:
            with self._engine.connect() as connection:
                row = (
                    connection.execute(
                        select(runs_table).where(runs_table.c.run_id == run_id)
                    )
                    .mappings()
                    .one_or_none()
                )
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError("Could not load run evidence") from error
        if row is None:
            raise RecordNotFoundError("Run was not found")
        return self._run_record(row)

    def list_runs(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        dataset_name: str | None = None,
    ) -> CursorPage[RunListRecord]:
        page_limit = _limit(limit)
        filters: dict[str, JsonValue] = {"dataset_name": dataset_name}
        statement = select(
            runs_table.c.run_id,
            runs_table.c.status,
            runs_table.c.execution_mode,
            runs_table.c.dataset_name,
            runs_table.c.dataset_revision,
            runs_table.c.result_digest,
            runs_table.c.created_at,
        )
        if dataset_name is not None:
            statement = statement.where(runs_table.c.dataset_name == dataset_name)
        if cursor is not None:
            key = _decode_cursor(cursor, stream="runs", filters=filters)
            if len(key) != 2 or not isinstance(key[1], str):
                raise InvalidCursorError("Pagination cursor is invalid")
            created_at = _decoded_cursor_time(key[0])
            statement = statement.where(
                (runs_table.c.created_at > created_at)
                | and_(
                    runs_table.c.created_at == created_at,
                    runs_table.c.run_id > key[1],
                )
            )
        statement = statement.order_by(
            runs_table.c.created_at, runs_table.c.run_id
        ).limit(page_limit + 1)
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(statement).mappings().all()
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError("Could not list runs") from error
        records = tuple(self._run_list_record(row) for row in rows[:page_limit])
        next_cursor = None
        if len(rows) > page_limit:
            next_cursor = _encode_cursor(
                stream="runs",
                filters=filters,
                key=[_cursor_time(records[-1].created_at), records[-1].run_id],
            )
        return CursorPage(items=records, next_cursor=next_cursor)

    def put_release_decision(
        self, record: ReleaseDecisionRecord
    ) -> ReleaseDecisionRecord:
        document = _model_text(record.decision)
        self._require_document_size(document)
        values = {
            "decision_id": record.decision_id,
            "decision_digest": record.decision.decision_digest,
            "baseline_run_id": record.decision.baseline_run_id,
            "candidate_run_id": record.decision.candidate_run_id,
            "status": record.decision.status.value,
            "document": document,
            "created_at": record.created_at,
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(insert(release_decisions_table).values(**values))
            return record
        except IntegrityError:
            try:
                existing = self.get_release_decision(record.decision_id)
            except RecordNotFoundError as error:
                raise ImmutableRecordConflictError(
                    "Release decision conflicts with existing metadata"
                ) from error
            if _model_text(existing.decision) != document:
                raise ImmutableRecordConflictError(
                    "Decision ID already contains different evidence"
                ) from None
            return existing
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError(
                "Could not store release decision"
            ) from error

    def get_release_decision(self, decision_id: str) -> ReleaseDecisionRecord:
        try:
            with self._engine.connect() as connection:
                row = (
                    connection.execute(
                        select(release_decisions_table).where(
                            release_decisions_table.c.decision_id == decision_id
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError(
                "Could not load release decision"
            ) from error
        if row is None:
            raise RecordNotFoundError("Release decision was not found")
        return self._release_decision_record(row)

    def list_release_decisions(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        status: ReleaseStatus | None = None,
    ) -> CursorPage[ReleaseDecisionListRecord]:
        page_limit = _limit(limit)
        filters: dict[str, JsonValue] = {
            "status": None if status is None else status.value
        }
        statement = select(
            release_decisions_table.c.decision_id,
            release_decisions_table.c.status,
            release_decisions_table.c.baseline_run_id,
            release_decisions_table.c.candidate_run_id,
            release_decisions_table.c.decision_digest,
            release_decisions_table.c.created_at,
        )
        if status is not None:
            statement = statement.where(
                release_decisions_table.c.status == status.value
            )
        if cursor is not None:
            key = _decode_cursor(cursor, stream="release-decisions", filters=filters)
            if len(key) != 2 or not isinstance(key[1], str):
                raise InvalidCursorError("Pagination cursor is invalid")
            created_at = _decoded_cursor_time(key[0])
            statement = statement.where(
                (release_decisions_table.c.created_at > created_at)
                | and_(
                    release_decisions_table.c.created_at == created_at,
                    release_decisions_table.c.decision_id > key[1],
                )
            )
        statement = statement.order_by(
            release_decisions_table.c.created_at,
            release_decisions_table.c.decision_id,
        ).limit(page_limit + 1)
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(statement).mappings().all()
        except SQLAlchemyError as error:
            raise ControlPlaneRepositoryError(
                "Could not list release decisions"
            ) from error
        records = tuple(
            self._release_decision_list_record(row) for row in rows[:page_limit]
        )
        next_cursor = None
        if len(rows) > page_limit:
            next_cursor = _encode_cursor(
                stream="release-decisions",
                filters=filters,
                key=[
                    _cursor_time(records[-1].created_at),
                    records[-1].decision_id,
                ],
            )
        return CursorPage(items=records, next_cursor=next_cursor)

    def _dataset_record(self, row: RowMapping) -> DatasetRecord:
        dataset = _validated_model(
            row["document"],
            DatasetVersion,
            max_document_bytes=self._max_document_bytes,
        )
        if (
            row["name"],
            row["revision"],
            row["digest"],
            row["case_count"],
        ) != (
            dataset.name,
            dataset.revision,
            dataset.digest,
            len(dataset.cases),
        ):
            raise CorruptRecordError(
                "Stored dataset indexes do not match canonical evidence"
            )
        try:
            return DatasetRecord(
                dataset=dataset,
                created_at=_aware(row["created_at"]),
            )
        except (KeyError, TypeError, ValidationError, ValueError) as error:
            raise CorruptRecordError(
                "Stored control-plane dataset is invalid"
            ) from error

    @staticmethod
    def _dataset_list_record(row: RowMapping) -> DatasetListRecord:
        try:
            return DatasetListRecord(
                name=row["name"],
                revision=row["revision"],
                digest=row["digest"],
                case_count=row["case_count"],
                created_at=_aware(row["created_at"]),
            )
        except (KeyError, TypeError, ValidationError, ValueError) as error:
            raise CorruptRecordError(
                "Stored control-plane dataset projection is invalid"
            ) from error

    @staticmethod
    def _job_record(row: RowMapping) -> JobRecord:
        try:
            return JobRecord(
                job_id=row["job_id"],
                kind=row["kind"],
                status=row["status"],
                idempotency_key=row["idempotency_key"],
                request_digest=row["request_digest"],
                resource_id=row["resource_id"],
                attempt_count=row["attempt_count"],
                max_attempts=row["max_attempts"],
                available_at=_aware(row["available_at"]),
                error_code=row["error_code"],
                created_at=_aware(row["created_at"]),
                updated_at=_aware(row["updated_at"]),
            )
        except (KeyError, TypeError, ValidationError, ValueError) as error:
            raise CorruptRecordError("Stored control-plane job is invalid") from error

    @staticmethod
    def _attempt_record(
        row: RowMapping | Mapping[str, Any],
    ) -> JobAttemptRecord:
        try:
            worker_id = row["worker_id"]
            lease_token = row["lease_token"]
            if (
                not isinstance(worker_id, str)
                or _WORKER_ID_PATTERN.fullmatch(worker_id) is None
                or not isinstance(lease_token, str)
                or _LEASE_TOKEN_PATTERN.fullmatch(lease_token) is None
            ):
                raise ValueError("stored private lease identity is invalid")
            return JobAttemptRecord(
                job_id=row["job_id"],
                attempt_number=row["attempt_number"],
                status=row["status"],
                error_code=row["error_code"],
                started_at=_aware(row["started_at"]),
                heartbeat_at=_aware(row["heartbeat_at"]),
                lease_expires_at=_aware(row["lease_expires_at"]),
                finished_at=(
                    None if row["finished_at"] is None else _aware(row["finished_at"])
                ),
            )
        except (KeyError, TypeError, ValidationError, ValueError) as error:
            raise CorruptRecordError(
                "Stored control-plane attempt is invalid"
            ) from error

    def _run_record(self, row: RowMapping) -> RunRecord:
        result = _validated_model(
            row["document"],
            RunResult,
            max_document_bytes=self._max_document_bytes,
        )
        if (
            row["run_id"],
            row["result_digest"],
            row["dataset_name"],
            row["dataset_revision"],
            row["status"],
            row["execution_mode"],
        ) != (
            result.run_id,
            result.result_digest,
            result.dataset.name,
            result.dataset.revision,
            result.status.value,
            result.execution_mode.value,
        ):
            raise CorruptRecordError(
                "Stored run indexes do not match canonical evidence"
            )
        try:
            return RunRecord(
                result=result,
                created_at=_aware(row["created_at"]),
            )
        except (KeyError, TypeError, ValidationError, ValueError) as error:
            raise CorruptRecordError("Stored control-plane run is invalid") from error

    @staticmethod
    def _run_list_record(row: RowMapping) -> RunListRecord:
        try:
            return RunListRecord(
                run_id=row["run_id"],
                status=row["status"],
                execution_mode=row["execution_mode"],
                dataset_name=row["dataset_name"],
                dataset_revision=row["dataset_revision"],
                result_digest=row["result_digest"],
                created_at=_aware(row["created_at"]),
            )
        except (KeyError, TypeError, ValidationError, ValueError) as error:
            raise CorruptRecordError(
                "Stored control-plane run projection is invalid"
            ) from error

    def _release_decision_record(self, row: RowMapping) -> ReleaseDecisionRecord:
        decision = _validated_model(
            row["document"],
            ReleaseDecision,
            max_document_bytes=self._max_document_bytes,
        )
        if (
            row["decision_digest"],
            row["baseline_run_id"],
            row["candidate_run_id"],
            row["status"],
        ) != (
            decision.decision_digest,
            decision.baseline_run_id,
            decision.candidate_run_id,
            decision.status.value,
        ):
            raise CorruptRecordError(
                "Stored decision indexes do not match canonical evidence"
            )
        try:
            return ReleaseDecisionRecord(
                decision_id=row["decision_id"],
                decision=decision,
                created_at=_aware(row["created_at"]),
            )
        except (KeyError, TypeError, ValidationError, ValueError) as error:
            raise CorruptRecordError(
                "Stored control-plane decision is invalid"
            ) from error

    @staticmethod
    def _release_decision_list_record(
        row: RowMapping,
    ) -> ReleaseDecisionListRecord:
        try:
            return ReleaseDecisionListRecord(
                decision_id=row["decision_id"],
                status=row["status"],
                baseline_run_id=row["baseline_run_id"],
                candidate_run_id=row["candidate_run_id"],
                decision_digest=row["decision_digest"],
                created_at=_aware(row["created_at"]),
            )
        except (KeyError, TypeError, ValidationError, ValueError) as error:
            raise CorruptRecordError(
                "Stored control-plane decision projection is invalid"
            ) from error
