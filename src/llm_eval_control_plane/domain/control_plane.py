"""Durable control-plane records and job lifecycle invariants."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Generic, Self, TypeVar

from pydantic import Field, field_validator, model_validator

from llm_eval_control_plane.domain.artifacts import Sha256Digest
from llm_eval_control_plane.domain.comparison import ReleaseDecision
from llm_eval_control_plane.domain.datasets import DatasetVersion
from llm_eval_control_plane.domain.execution import RunId, SafeCode
from llm_eval_control_plane.domain.models import FrozenModel
from llm_eval_control_plane.domain.results import RunResult

StableId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
IdempotencyKey = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
Cursor = Annotated[str, Field(min_length=1, max_length=2048)]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("control-plane timestamps must include a timezone")
    return value.astimezone(UTC)


class DatasetRecord(FrozenModel):
    """One immutable dataset revision and its registration time."""

    dataset: DatasetVersion
    created_at: datetime

    _normalize_created_at = field_validator("created_at")(_utc)


class JobKind(StrEnum):
    RUN = "run"
    COMPARISON = "comparison"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset((JobStatus.RUNNING,)),
    JobStatus.RUNNING: frozenset((JobStatus.SUCCEEDED, JobStatus.FAILED)),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
}


class JobTransitionError(ValueError):
    """Raised when a job lifecycle transition violates the state machine."""


class JobRecord(FrozenModel):
    """Durable idempotent work submission and its current lifecycle state."""

    job_id: StableId
    kind: JobKind
    status: JobStatus
    idempotency_key: IdempotencyKey
    request_digest: Sha256Digest
    resource_id: StableId
    error_code: SafeCode | None = None
    created_at: datetime
    updated_at: datetime

    _normalize_created_at = field_validator("created_at")(_utc)
    _normalize_updated_at = field_validator("updated_at")(_utc)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("job update time cannot precede creation time")
        if (self.status is JobStatus.FAILED) != (self.error_code is not None):
            raise ValueError("only failed jobs contain an error code")
        return self

    def transition_to(
        self,
        status: JobStatus,
        *,
        at: datetime,
        error_code: SafeCode | None = None,
    ) -> JobRecord:
        """Return the next legal state, accepting exact-state retries."""
        normalized_at = _utc(at)
        if status is self.status:
            if error_code != self.error_code:
                raise JobTransitionError("job retry does not match the stored state")
            return self
        if status not in _ALLOWED_TRANSITIONS[self.status]:
            raise JobTransitionError("job lifecycle transition is not allowed")
        if normalized_at < self.updated_at:
            raise JobTransitionError("job transition time cannot move backwards")
        if (status is JobStatus.FAILED) != (error_code is not None):
            raise JobTransitionError("failed transitions require one safe error code")
        return JobRecord.model_validate(
            {
                **self.model_dump(mode="python"),
                "status": status,
                "error_code": error_code,
                "updated_at": normalized_at,
            }
        )


class RunRecord(FrozenModel):
    """Append-only canonical evidence for one completed evaluation run."""

    result: RunResult
    created_at: datetime

    _normalize_created_at = field_validator("created_at")(_utc)

    @property
    def run_id(self) -> RunId:
        return self.result.run_id


class ReleaseDecisionRecord(FrozenModel):
    """Append-only canonical evidence for one baseline comparison decision."""

    decision_id: StableId
    decision: ReleaseDecision
    created_at: datetime

    _normalize_created_at = field_validator("created_at")(_utc)


PageItem = TypeVar("PageItem")


class CursorPage(FrozenModel, Generic[PageItem]):
    """One stable keyset page and an opaque continuation cursor."""

    items: tuple[PageItem, ...]
    next_cursor: Cursor | None = None
