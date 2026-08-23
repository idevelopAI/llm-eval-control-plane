"""Durable control-plane records and job lifecycle invariants."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Generic, Literal, Self, TypeAlias, TypeVar

from pydantic import (
    Field,
    NonNegativeInt,
    PositiveInt,
    field_validator,
    model_validator,
)

from llm_eval_control_plane.domain.artifacts import (
    ArtifactKind,
    ArtifactName,
    ArtifactRef,
    Sha256Digest,
)
from llm_eval_control_plane.domain.canonical import sha256_digest
from llm_eval_control_plane.domain.comparison import ReleaseDecision, ReleaseStatus
from llm_eval_control_plane.domain.datasets import DatasetVersion
from llm_eval_control_plane.domain.evaluation import EvaluationSpec
from llm_eval_control_plane.domain.execution import RunId, SafeCode
from llm_eval_control_plane.domain.models import FrozenModel
from llm_eval_control_plane.domain.results import ExecutionMode, RunResult, RunStatus

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
WorkerId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
LeaseToken = Annotated[
    str,
    Field(
        min_length=32,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        repr=False,
    ),
]
CaseId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
ScenarioName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
MaxAttempts = Annotated[int, Field(ge=1, le=10)]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("control-plane timestamps must include a timezone")
    return value.astimezone(UTC)


class DatasetRecord(FrozenModel):
    """One immutable dataset revision and its registration time."""

    dataset: DatasetVersion
    created_at: datetime

    _normalize_created_at = field_validator("created_at")(_utc)


class DatasetListRecord(FrozenModel):
    """Validated metadata projection for a dataset collection item."""

    name: ArtifactName
    revision: PositiveInt
    digest: Sha256Digest
    case_count: PositiveInt
    created_at: datetime

    _normalize_created_at = field_validator("created_at")(_utc)


class JobKind(StrEnum):
    RUN = "run"
    COMPARISON = "comparison"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset((JobStatus.RUNNING, JobStatus.CANCELED)),
    JobStatus.RUNNING: frozenset(
        (
            JobStatus.QUEUED,
            JobStatus.CANCEL_REQUESTED,
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
        )
    ),
    JobStatus.CANCEL_REQUESTED: frozenset((JobStatus.CANCELED,)),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELED: frozenset(),
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
    attempt_count: NonNegativeInt
    max_attempts: MaxAttempts
    available_at: datetime
    error_code: SafeCode | None = None
    created_at: datetime
    updated_at: datetime

    _normalize_available_at = field_validator("available_at")(_utc)
    _normalize_created_at = field_validator("created_at")(_utc)
    _normalize_updated_at = field_validator("updated_at")(_utc)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("job update time cannot precede creation time")
        if self.available_at < self.created_at:
            raise ValueError("job availability cannot precede creation time")
        if self.attempt_count > self.max_attempts:
            raise ValueError("job attempts cannot exceed the configured maximum")
        if self.status is JobStatus.QUEUED and self.attempt_count >= self.max_attempts:
            raise ValueError("queued jobs must have an attempt remaining")
        if (
            self.status
            in {
                JobStatus.RUNNING,
                JobStatus.CANCEL_REQUESTED,
                JobStatus.SUCCEEDED,
                JobStatus.FAILED,
            }
            and self.attempt_count < 1
        ):
            raise ValueError("started or completed jobs require an attempt")
        if (self.status is JobStatus.FAILED) != (self.error_code is not None):
            raise ValueError("only failed jobs contain an error code")
        return self

    def transition_to(
        self,
        status: JobStatus,
        *,
        at: datetime,
        error_code: SafeCode | None = None,
        available_at: datetime | None = None,
    ) -> JobRecord:
        """Return one legal lifecycle change, accepting exact-state retries."""
        normalized_at = _utc(at)
        if status is self.status:
            normalized_available_at = (
                None if available_at is None else _utc(available_at)
            )
            if error_code != self.error_code or normalized_available_at not in {
                None,
                self.available_at,
            }:
                raise JobTransitionError("job retry does not match the stored state")
            return self
        if status not in _ALLOWED_TRANSITIONS[self.status]:
            raise JobTransitionError("job lifecycle transition is not allowed")
        if normalized_at < self.updated_at:
            raise JobTransitionError("job transition time cannot move backwards")
        if (status is JobStatus.FAILED) != (error_code is not None):
            raise JobTransitionError("failed transitions require one safe error code")

        next_attempt_count = self.attempt_count
        next_available_at = self.available_at
        if self.status is JobStatus.QUEUED and status is JobStatus.RUNNING:
            if normalized_at < self.available_at:
                raise JobTransitionError("job is not available to claim")
            if self.attempt_count >= self.max_attempts:
                raise JobTransitionError("job has no attempt remaining")
            if available_at is not None:
                raise JobTransitionError("claim cannot reschedule job availability")
            next_attempt_count += 1
        elif self.status is JobStatus.RUNNING and status is JobStatus.QUEUED:
            if available_at is None:
                raise JobTransitionError("retry requires a future availability time")
            next_available_at = _utc(available_at)
            if next_available_at < normalized_at:
                raise JobTransitionError("retry availability cannot precede transition")
            if self.attempt_count >= self.max_attempts:
                raise JobTransitionError("job has no retry attempt remaining")
        elif available_at is not None:
            raise JobTransitionError("only retry transitions set job availability")

        return JobRecord.model_validate(
            {
                **self.model_dump(mode="python"),
                "status": status,
                "attempt_count": next_attempt_count,
                "available_at": next_available_at,
                "error_code": error_code,
                "updated_at": normalized_at,
            }
        )

    def request_cancellation(self, *, at: datetime) -> JobRecord:
        """Cancel queued work or request cooperative cancellation of an attempt."""
        if self.status is JobStatus.QUEUED:
            return self.transition_to(JobStatus.CANCELED, at=at)
        if self.status is JobStatus.RUNNING:
            return self.transition_to(JobStatus.CANCEL_REQUESTED, at=at)
        return self


class ExecutionContract(FrozenModel):
    """Resolved immutable evidence identities promised by an executor."""

    adapter: StableId
    evaluator_names: tuple[StableId, ...]
    target: ArtifactRef
    evaluators: tuple[ArtifactRef, ...]
    execution_mode: ExecutionMode

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if not self.evaluator_names:
            raise ValueError("execution contract requires an evaluator")
        if len(self.evaluator_names) != len(set(self.evaluator_names)):
            raise ValueError("execution contract evaluator names must be unique")
        if self.target.kind is not ArtifactKind.TARGET or self.target.digest is None:
            raise ValueError("execution contract target must be resolved")
        if len(self.evaluators) != len(self.evaluator_names):
            raise ValueError("execution contract evaluator count does not match")
        if any(
            evaluator.kind is not ArtifactKind.EVALUATOR or evaluator.digest is None
            for evaluator in self.evaluators
        ):
            raise ValueError("execution contract evaluators must be resolved")
        evaluator_keys = [evaluator.logical_key for evaluator in self.evaluators]
        if len(evaluator_keys) != len(set(evaluator_keys)):
            raise ValueError("execution contract evaluators must be unique")
        if evaluator_keys != sorted(evaluator_keys):
            raise ValueError("execution contract evaluators must be ordered")
        return self


class ScenarioOverride(FrozenModel):
    """One bounded deterministic scenario override stored as worker input."""

    case_id: CaseId
    scenario: ScenarioName


class RunJobPayload(FrozenModel):
    """Canonical resolved worker input for one evaluation run."""

    schema_version: Literal["run-job/v1"] = "run-job/v1"
    kind: Literal[JobKind.RUN] = JobKind.RUN
    dataset: ArtifactRef
    target_name: ArtifactName
    target_revision: PositiveInt
    adapter: StableId
    evaluator_names: tuple[StableId, ...]
    scenario_overrides: tuple[ScenarioOverride, ...] = ()
    execution_contract: ExecutionContract

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if self.dataset.kind is not ArtifactKind.DATASET or self.dataset.digest is None:
            raise ValueError("run job dataset must be resolved")
        case_ids = [override.case_id for override in self.scenario_overrides]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("run job scenario overrides must be unique")
        if case_ids != sorted(case_ids):
            raise ValueError("run job scenario overrides must be ordered")
        contract = self.execution_contract
        if contract.adapter != self.adapter:
            raise ValueError("run job adapter does not match execution contract")
        if contract.evaluator_names != self.evaluator_names:
            raise ValueError("run job evaluators do not match execution contract")
        if (
            contract.target.name != self.target_name
            or contract.target.revision != self.target_revision
        ):
            raise ValueError("run job target does not match execution contract")
        return self

    @property
    def payload_digest(self) -> Sha256Digest:
        return sha256_digest(self.model_dump(mode="json"))


class ComparisonJobPayload(FrozenModel):
    """Canonical resolved worker input for one baseline comparison."""

    schema_version: Literal["comparison-job/v1"] = "comparison-job/v1"
    kind: Literal[JobKind.COMPARISON] = JobKind.COMPARISON
    dataset: ArtifactRef
    baseline_run_id: RunId
    baseline_result_digest: Sha256Digest
    candidate_run_id: RunId
    candidate_result_digest: Sha256Digest
    spec: EvaluationSpec

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if self.dataset.kind is not ArtifactKind.DATASET or self.dataset.digest is None:
            raise ValueError("comparison job dataset must be resolved")
        if self.spec.dataset != self.dataset:
            raise ValueError("comparison job dataset does not match its policy")
        if self.baseline_run_id == self.candidate_run_id:
            raise ValueError("comparison job runs must be distinct")
        return self

    @property
    def payload_digest(self) -> Sha256Digest:
        return sha256_digest(self.model_dump(mode="json"))


JobPayload: TypeAlias = Annotated[
    RunJobPayload | ComparisonJobPayload,
    Field(discriminator="kind"),
]


class JobAttemptStatus(StrEnum):
    """Durable outcome of one fenced execution attempt."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    CANCELED = "canceled"
    LEASE_EXPIRED = "lease_expired"


class JobAttemptRecord(FrozenModel):
    """Safe durable metadata for one claimed attempt; lease tokens stay private."""

    job_id: StableId
    attempt_number: PositiveInt
    status: JobAttemptStatus
    error_code: SafeCode | None = None
    started_at: datetime
    heartbeat_at: datetime
    lease_expires_at: datetime
    finished_at: datetime | None = None

    _normalize_started_at = field_validator("started_at")(_utc)
    _normalize_heartbeat_at = field_validator("heartbeat_at")(_utc)
    _normalize_lease_expires_at = field_validator("lease_expires_at")(_utc)
    _normalize_finished_at = field_validator("finished_at")(
        lambda value: None if value is None else _utc(value)
    )

    @model_validator(mode="after")
    def validate_attempt(self) -> Self:
        if self.heartbeat_at < self.started_at:
            raise ValueError("attempt heartbeat cannot precede its start")
        if self.lease_expires_at <= self.heartbeat_at:
            raise ValueError("attempt lease must expire after its heartbeat")
        is_running = self.status is JobAttemptStatus.RUNNING
        if is_running != (self.finished_at is None):
            raise ValueError("only running attempts omit a finish time")
        if self.finished_at is not None and self.finished_at < self.heartbeat_at:
            raise ValueError("attempt finish cannot precede its heartbeat")
        failed_statuses = {
            JobAttemptStatus.RETRY_SCHEDULED,
            JobAttemptStatus.FAILED,
            JobAttemptStatus.LEASE_EXPIRED,
        }
        if (self.status in failed_statuses) != (self.error_code is not None):
            raise ValueError("failed attempts require one safe error code")
        return self


class RunRecord(FrozenModel):
    """Append-only canonical evidence for one completed evaluation run."""

    result: RunResult
    created_at: datetime

    _normalize_created_at = field_validator("created_at")(_utc)

    @property
    def run_id(self) -> RunId:
        return self.result.run_id


class RunListRecord(FrozenModel):
    """Validated metadata projection for a run collection item."""

    run_id: RunId
    status: RunStatus
    execution_mode: ExecutionMode
    dataset_name: ArtifactName
    dataset_revision: PositiveInt
    result_digest: Sha256Digest
    created_at: datetime

    _normalize_created_at = field_validator("created_at")(_utc)


class ReleaseDecisionRecord(FrozenModel):
    """Append-only canonical evidence for one baseline comparison decision."""

    decision_id: StableId
    decision: ReleaseDecision
    created_at: datetime

    _normalize_created_at = field_validator("created_at")(_utc)


class ReleaseDecisionListRecord(FrozenModel):
    """Validated metadata projection for a release-decision collection item."""

    decision_id: StableId
    status: ReleaseStatus
    baseline_run_id: RunId
    candidate_run_id: RunId
    decision_digest: Sha256Digest
    created_at: datetime

    _normalize_created_at = field_validator("created_at")(_utc)


PageItem = TypeVar("PageItem")


class CursorPage(FrozenModel, Generic[PageItem]):
    """One stable keyset page and an opaque continuation cursor."""

    items: tuple[PageItem, ...]
    next_cursor: Cursor | None = None
