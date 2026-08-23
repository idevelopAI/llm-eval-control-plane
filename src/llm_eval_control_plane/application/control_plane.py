"""Durable control-plane use cases over an injected metadata repository."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from llm_eval_control_plane.domain.canonical import JsonValue, sha256_digest
from llm_eval_control_plane.domain.comparison import ReleaseStatus
from llm_eval_control_plane.domain.control_plane import (
    ComparisonJobPayload,
    CursorPage,
    DatasetListRecord,
    DatasetRecord,
    ExecutionContract,
    JobAttemptRecord,
    JobKind,
    JobPayload,
    JobRecord,
    JobStatus,
    LeaseToken,
    ReleaseDecisionListRecord,
    ReleaseDecisionRecord,
    RunJobPayload,
    RunListRecord,
    RunRecord,
    ScenarioOverride,
    WorkerId,
)
from llm_eval_control_plane.domain.datasets import DatasetVersion
from llm_eval_control_plane.domain.evaluation import EvaluationSpec
from llm_eval_control_plane.domain.results import RunResult


class ControlPlaneStoreError(RuntimeError):
    """Base error exposed by control-plane persistence adapters."""


class StoreNotFoundError(ControlPlaneStoreError):
    """A requested immutable record does not exist."""


class StoreConflictError(ControlPlaneStoreError):
    """An immutable identity already contains different content."""


class StoreIdempotencyConflictError(StoreConflictError):
    """An idempotency key was reused for a different canonical request."""


class StoreInvalidCursorError(ControlPlaneStoreError):
    """A pagination cursor failed integrity or contract validation."""


class StoreTransitionError(ControlPlaneStoreError):
    """A job status compare-and-set could not be applied."""


class StoreLeaseLostError(StoreTransitionError):
    """A worker no longer owns the bounded lease for an execution attempt."""


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """One private fenced worker claim and its immutable resolved payload."""

    job: JobRecord
    payload: JobPayload
    attempt: JobAttemptRecord
    lease_token: LeaseToken = field(repr=False)


class ControlPlaneRepository(Protocol):
    """Persistence operations required by the application service."""

    def put_dataset(self, record: DatasetRecord) -> DatasetRecord: ...

    def get_dataset(self, name: str, revision: int) -> DatasetRecord: ...

    def list_datasets(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        name: str | None = None,
    ) -> CursorPage[DatasetListRecord]: ...

    def begin_job(
        self,
        record: JobRecord,
        payload: JobPayload,
    ) -> tuple[JobRecord, bool]: ...

    def get_job(self, job_id: str) -> JobRecord: ...

    def list_jobs(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        kind: JobKind | None = None,
        status: JobStatus | None = None,
    ) -> CursorPage[JobRecord]: ...

    def claim_next_job(
        self,
        *,
        worker_id: WorkerId,
        lease_token: LeaseToken,
        lease_seconds: int,
    ) -> ClaimedJob | None: ...

    def heartbeat_job(
        self,
        job_id: str,
        attempt_number: int,
        lease_token: LeaseToken,
        *,
        lease_seconds: int,
    ) -> JobRecord: ...

    def retry_job(
        self,
        job_id: str,
        attempt_number: int,
        lease_token: LeaseToken,
        *,
        delay_seconds: int,
        error_code: str,
    ) -> JobRecord: ...

    def fail_job(
        self,
        job_id: str,
        attempt_number: int,
        lease_token: LeaseToken,
        *,
        error_code: str,
    ) -> JobRecord: ...

    def reap_expired_jobs(
        self,
        *,
        limit: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
    ) -> tuple[JobRecord, ...]: ...

    def cancel_job(self, job_id: str) -> JobRecord: ...

    def acknowledge_cancellation(
        self,
        job_id: str,
        attempt_number: int,
        lease_token: LeaseToken,
    ) -> JobRecord: ...

    def list_job_attempts(self, job_id: str) -> tuple[JobAttemptRecord, ...]: ...

    def complete_run(
        self,
        job_id: str,
        record: RunRecord,
        *,
        attempt_number: int,
        lease_token: LeaseToken,
    ) -> JobRecord: ...

    def get_run(self, run_id: str) -> RunRecord: ...

    def list_runs(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        dataset_name: str | None = None,
    ) -> CursorPage[RunListRecord]: ...

    def complete_release_decision(
        self,
        job_id: str,
        record: ReleaseDecisionRecord,
        *,
        attempt_number: int,
        lease_token: LeaseToken,
    ) -> JobRecord: ...

    def get_release_decision(self, decision_id: str) -> ReleaseDecisionRecord: ...

    def list_release_decisions(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        status: ReleaseStatus | None = None,
    ) -> CursorPage[ReleaseDecisionListRecord]: ...

    def check_health(self) -> None: ...

    def schema_is_current(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class RunSubmission:
    """Validated parameters for one deterministic in-process evaluation."""

    idempotency_key: str
    dataset_name: str
    dataset_revision: int
    target_name: str
    target_revision: int
    adapter: str
    evaluator_names: tuple[str, ...]
    scenario_overrides: Mapping[str, str]

    def digest_record(self) -> dict[str, JsonValue]:
        """Return the request semantics covered by idempotency."""
        return {
            "adapter": self.adapter,
            "dataset": {
                "name": self.dataset_name,
                "revision": self.dataset_revision,
            },
            "evaluators": list(self.evaluator_names),
            "scenario_overrides": dict(sorted(self.scenario_overrides.items())),
            "target": {
                "name": self.target_name,
                "revision": self.target_revision,
            },
        }


@dataclass(frozen=True, slots=True)
class ComparisonSubmission:
    """Validated parameters for a stored baseline/candidate decision."""

    idempotency_key: str
    dataset_name: str
    dataset_revision: int
    baseline_run_id: str
    candidate_run_id: str
    spec: EvaluationSpec

    def digest_record(self) -> dict[str, JsonValue]:
        """Return the request semantics covered by idempotency."""
        return {
            "baseline_run_id": self.baseline_run_id,
            "candidate_run_id": self.candidate_run_id,
            "dataset": {
                "name": self.dataset_name,
                "revision": self.dataset_revision,
            },
            "spec": self.spec.model_dump(mode="json"),
        }


class EvaluationExecutor(Protocol):
    """Resolve adapters and run one evaluation without owning persistence."""

    def validate(
        self,
        *,
        target_name: str,
        target_revision: int,
        adapter: str,
        evaluator_names: tuple[str, ...],
        scenario_overrides: Mapping[str, str],
    ) -> ExecutionContract: ...

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
    ) -> RunResult: ...


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    """A durable job and whether this request inserted it."""

    job: JobRecord
    created: bool


class ControlPlaneServiceError(RuntimeError):
    """Content-safe application failure suitable for HTTP translation."""

    code = "control_plane_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ResourceNotFoundError(ControlPlaneServiceError):
    code = "resource_not_found"


class ResourceConflictError(ControlPlaneServiceError):
    code = "resource_conflict"


class IdempotencyConflictError(ResourceConflictError):
    code = "idempotency_conflict"


class InvalidCursorError(ControlPlaneServiceError):
    code = "invalid_cursor"


class InvalidSubmissionError(ControlPlaneServiceError):
    code = "invalid_submission"


Clock = Callable[[], datetime]
IdentifierFactory = Callable[[str], str]

_MAX_DATASET_CASES = 1_000
_MAX_SLICES_PER_CASE = 32
_MAX_DATASET_SLICES = 128
_MAX_COMPARISON_GATES = 64
_MAX_COMPARISON_METRICS = 32
_MAX_COMPARISON_CASE_RECORDS = 50_000
_MAX_COMPARISON_AGGREGATE_WORK = 2_000_000


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _identifier(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


class ControlPlaneService:
    """Coordinate immutable metadata and durable asynchronous submissions."""

    def __init__(
        self,
        *,
        repository: ControlPlaneRepository,
        executor: EvaluationExecutor,
        clock: Clock = _utc_now,
        identifier_factory: IdentifierFactory = _identifier,
        max_attempts: int = 3,
    ) -> None:
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError("maximum attempts must be between 1 and 10")
        self._repository = repository
        self._executor = executor
        self._clock = clock
        self._identifier_factory = identifier_factory
        self._max_attempts = max_attempts

    def register_dataset(self, dataset: DatasetVersion) -> DatasetRecord:
        """Append one immutable dataset revision, allowing byte-identical retries."""
        try:
            self._validate_dataset_bounds(dataset)
        except ValueError as error:
            raise InvalidSubmissionError(
                "Dataset revision exceeds service limits"
            ) from error
        record = DatasetRecord(dataset=dataset, created_at=self._clock())
        try:
            return self._repository.put_dataset(record)
        except StoreConflictError as error:
            raise ResourceConflictError(
                "Dataset revision already contains different content"
            ) from error

    def get_dataset(self, name: str, revision: int) -> DatasetRecord:
        try:
            return self._repository.get_dataset(name, revision)
        except StoreNotFoundError as error:
            raise ResourceNotFoundError("Dataset revision was not found") from error

    def list_datasets(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        name: str | None = None,
    ) -> CursorPage[DatasetListRecord]:
        try:
            return self._repository.list_datasets(
                limit=limit,
                cursor=cursor,
                name=name,
            )
        except StoreInvalidCursorError as error:
            raise InvalidCursorError("Pagination cursor is invalid") from error

    async def submit_run(self, submission: RunSubmission) -> SubmissionResult:
        """Validate and atomically enqueue one idempotent evaluation job."""
        try:
            dataset_record = self._repository.get_dataset(
                submission.dataset_name,
                submission.dataset_revision,
            )
        except StoreNotFoundError as error:
            raise ResourceNotFoundError("Dataset revision was not found") from error
        try:
            execution_contract = self._executor.validate(
                target_name=submission.target_name,
                target_revision=submission.target_revision,
                adapter=submission.adapter,
                evaluator_names=submission.evaluator_names,
                scenario_overrides=submission.scenario_overrides,
            )
            validate_execution_contract(
                execution_contract,
                target_name=submission.target_name,
                target_revision=submission.target_revision,
                adapter=submission.adapter,
                evaluator_names=submission.evaluator_names,
            )
            payload = RunJobPayload(
                dataset=dataset_record.dataset.artifact_ref,
                target_name=submission.target_name,
                target_revision=submission.target_revision,
                adapter=submission.adapter,
                evaluator_names=submission.evaluator_names,
                scenario_overrides=tuple(
                    ScenarioOverride(case_id=case_id, scenario=scenario)
                    for case_id, scenario in sorted(
                        submission.scenario_overrides.items()
                    )
                ),
                execution_contract=execution_contract,
            )
        except ValueError as error:
            raise InvalidSubmissionError("Run submission is invalid") from error

        request_digest = sha256_digest(submission.digest_record())
        now = self._clock()
        proposed = JobRecord(
            job_id=self._identifier_factory("job"),
            kind=JobKind.RUN,
            status=JobStatus.QUEUED,
            idempotency_key=submission.idempotency_key,
            request_digest=request_digest,
            resource_id=self._identifier_factory("run"),
            attempt_count=0,
            max_attempts=self._max_attempts,
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        try:
            job, created = self._repository.begin_job(proposed, payload)
        except StoreIdempotencyConflictError as error:
            raise IdempotencyConflictError(
                "Idempotency key was used for a different request"
            ) from error
        except StoreConflictError as error:
            raise ResourceConflictError("Job identity already exists") from error
        return SubmissionResult(job=job, created=created)

    def get_job(self, job_id: str) -> JobRecord:
        try:
            return self._repository.get_job(job_id)
        except StoreNotFoundError as error:
            raise ResourceNotFoundError("Job was not found") from error

    def list_jobs(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        kind: JobKind | None = None,
        status: JobStatus | None = None,
    ) -> CursorPage[JobRecord]:
        try:
            return self._repository.list_jobs(
                limit=limit,
                cursor=cursor,
                kind=kind,
                status=status,
            )
        except StoreInvalidCursorError as error:
            raise InvalidCursorError("Pagination cursor is invalid") from error

    def get_run(self, run_id: str) -> RunRecord:
        try:
            return self._repository.get_run(run_id)
        except StoreNotFoundError as error:
            raise ResourceNotFoundError("Run was not found") from error

    def list_runs(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        dataset_name: str | None = None,
    ) -> CursorPage[RunListRecord]:
        try:
            return self._repository.list_runs(
                limit=limit,
                cursor=cursor,
                dataset_name=dataset_name,
            )
        except StoreInvalidCursorError as error:
            raise InvalidCursorError("Pagination cursor is invalid") from error

    async def submit_comparison(
        self,
        submission: ComparisonSubmission,
    ) -> SubmissionResult:
        """Validate and atomically enqueue one idempotent comparison job."""
        try:
            dataset = self._repository.get_dataset(
                submission.dataset_name,
                submission.dataset_revision,
            )
            baseline = self._repository.get_run(submission.baseline_run_id)
            candidate = self._repository.get_run(submission.candidate_run_id)
            validate_comparison_inputs(
                dataset_name=submission.dataset_name,
                dataset_revision=submission.dataset_revision,
                baseline_run_id=submission.baseline_run_id,
                candidate_run_id=submission.candidate_run_id,
                spec=submission.spec,
                dataset=dataset,
                baseline=baseline,
                candidate=candidate,
            )
            payload = ComparisonJobPayload(
                dataset=dataset.dataset.artifact_ref,
                baseline_run_id=baseline.result.run_id,
                baseline_result_digest=baseline.result.result_digest,
                candidate_run_id=candidate.result.run_id,
                candidate_result_digest=candidate.result.result_digest,
                spec=submission.spec,
            )
        except StoreNotFoundError as error:
            raise ResourceNotFoundError("Comparison evidence was not found") from error
        except ValueError as error:
            raise InvalidSubmissionError("Comparison submission is invalid") from error

        request_digest = sha256_digest(submission.digest_record())
        now = self._clock()
        proposed = JobRecord(
            job_id=self._identifier_factory("job"),
            kind=JobKind.COMPARISON,
            status=JobStatus.QUEUED,
            idempotency_key=submission.idempotency_key,
            request_digest=request_digest,
            resource_id=self._identifier_factory("decision"),
            attempt_count=0,
            max_attempts=self._max_attempts,
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        try:
            job, created = self._repository.begin_job(proposed, payload)
        except StoreIdempotencyConflictError as error:
            raise IdempotencyConflictError(
                "Idempotency key was used for a different request"
            ) from error
        except StoreConflictError as error:
            raise ResourceConflictError("Job identity already exists") from error
        return SubmissionResult(job=job, created=created)

    def cancel_job(self, job_id: str) -> JobRecord:
        """Request idempotent cooperative cancellation for one durable job."""
        try:
            return self._repository.cancel_job(job_id)
        except StoreNotFoundError as error:
            raise ResourceNotFoundError("Job was not found") from error
        except StoreTransitionError as error:
            raise ResourceConflictError("Job cannot be canceled") from error

    def list_job_attempts(self, job_id: str) -> tuple[JobAttemptRecord, ...]:
        """Return bounded attempt history after proving that the job exists."""
        self.get_job(job_id)
        return self._repository.list_job_attempts(job_id)

    def get_release_decision(self, decision_id: str) -> ReleaseDecisionRecord:
        try:
            return self._repository.get_release_decision(decision_id)
        except StoreNotFoundError as error:
            raise ResourceNotFoundError("Release decision was not found") from error

    def list_release_decisions(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        status: ReleaseStatus | None = None,
    ) -> CursorPage[ReleaseDecisionListRecord]:
        try:
            return self._repository.list_release_decisions(
                limit=limit,
                cursor=cursor,
                status=status,
            )
        except StoreInvalidCursorError as error:
            raise InvalidCursorError("Pagination cursor is invalid") from error

    def ready(self) -> bool:
        """Return a content-free readiness result for the HTTP health endpoint."""
        try:
            self._repository.check_health()
            if not self._repository.schema_is_current():
                return False
        except Exception:
            return False
        return True

    @staticmethod
    def _validate_dataset_bounds(dataset: DatasetVersion) -> None:
        if len(dataset.cases) > _MAX_DATASET_CASES:
            raise ValueError("dataset contains too many cases")
        if any(len(case.slices) > _MAX_SLICES_PER_CASE for case in dataset.cases):
            raise ValueError("dataset case contains too many slices")
        slices = {label for case in dataset.cases for label in case.slices}
        if len(slices) > _MAX_DATASET_SLICES:
            raise ValueError("dataset contains too many unique slices")


def validate_execution_contract(
    contract: ExecutionContract,
    *,
    target_name: str,
    target_revision: int,
    adapter: str,
    evaluator_names: tuple[str, ...],
) -> None:
    """Verify that a resolved executor contract matches durable worker input."""
    if contract.adapter != adapter:
        raise ValueError("executor resolved a different adapter")
    if contract.evaluator_names != evaluator_names:
        raise ValueError("executor resolved different evaluators")
    if (
        contract.target.name != target_name
        or contract.target.revision != target_revision
    ):
        raise ValueError("executor resolved a different target")


def validate_run_result(
    result: RunResult,
    *,
    resource_id: str,
    dataset: DatasetRecord,
    contract: ExecutionContract,
) -> None:
    """Reject executor evidence that escapes its pinned durable contract."""
    if result.run_id != resource_id:
        raise ValueError("executor returned an unexpected run identity")
    if result.dataset != dataset.dataset.artifact_ref:
        raise ValueError("executor returned an unexpected dataset identity")
    if result.target != contract.target:
        raise ValueError("executor returned an unexpected target identity")
    if result.evaluators != contract.evaluators:
        raise ValueError("executor returned unexpected evaluator identities")
    allowed_evaluators = set(contract.evaluators)
    if any(summary.evaluator not in allowed_evaluators for summary in result.metrics):
        raise ValueError("executor returned an unexpected metric evaluator")
    if any(
        observation.evaluator not in allowed_evaluators
        for case in result.cases
        for observation in case.observations
    ):
        raise ValueError("executor returned an unexpected observation evaluator")
    if any(
        failure.evaluator is not None and failure.evaluator not in allowed_evaluators
        for case in result.cases
        for failure in case.evaluator_failures
    ):
        raise ValueError("executor returned an unexpected failure evaluator")
    if result.execution_mode is not contract.execution_mode:
        raise ValueError("executor returned an unexpected execution mode")
    expected_case_ids = tuple(case.case_id for case in dataset.dataset.cases)
    actual_case_ids = tuple(case.case_id for case in result.cases)
    if actual_case_ids != expected_case_ids:
        raise ValueError("executor returned an unexpected case set")


def validate_comparison_inputs(
    *,
    dataset_name: str,
    dataset_revision: int,
    baseline_run_id: str,
    candidate_run_id: str,
    spec: EvaluationSpec,
    dataset: DatasetRecord,
    baseline: RunRecord,
    candidate: RunRecord,
) -> None:
    """Validate immutable comparison inputs before enqueueing or executing."""
    ControlPlaneService._validate_dataset_bounds(dataset.dataset)
    if len(spec.gates) > _MAX_COMPARISON_GATES:
        raise ValueError("comparison contains too many gates")
    metric_count = len(baseline.result.metrics)
    if metric_count > _MAX_COMPARISON_METRICS:
        raise ValueError("comparison contains too many metrics")
    slices = {label for case in dataset.dataset.cases for label in case.slices}
    aggregate_work = metric_count * (len(slices) + 1) * len(dataset.dataset.cases)
    if aggregate_work > _MAX_COMPARISON_AGGREGATE_WORK:
        raise ValueError("comparison aggregate work exceeds service limits")
    case_records = sum(
        len(dataset.dataset.cases)
        if gate.slice is None
        else sum(gate.slice in case.slices for case in dataset.dataset.cases)
        for gate in spec.gates
    )
    if case_records > _MAX_COMPARISON_CASE_RECORDS:
        raise ValueError("comparison evidence exceeds service limits")
    if (
        dataset.dataset.name != dataset_name
        or dataset.dataset.revision != dataset_revision
    ):
        raise ValueError("comparison dataset identity does not match")
    if spec.dataset != dataset.dataset.artifact_ref:
        raise ValueError("spec references a different dataset revision")
    if baseline.result.run_id != baseline_run_id:
        raise ValueError("baseline run identity does not match")
    if candidate.result.run_id != candidate_run_id:
        raise ValueError("candidate run identity does not match")
    if spec.baseline != baseline.result.target:
        raise ValueError("spec baseline does not match baseline run")
    if spec.candidate != candidate.result.target:
        raise ValueError("spec candidate does not match candidate run")


__all__ = [
    "ClaimedJob",
    "ComparisonSubmission",
    "ControlPlaneRepository",
    "ControlPlaneService",
    "ControlPlaneServiceError",
    "ControlPlaneStoreError",
    "EvaluationExecutor",
    "ExecutionContract",
    "IdempotencyConflictError",
    "InvalidCursorError",
    "InvalidSubmissionError",
    "ResourceConflictError",
    "ResourceNotFoundError",
    "RunSubmission",
    "StoreConflictError",
    "StoreIdempotencyConflictError",
    "StoreInvalidCursorError",
    "StoreLeaseLostError",
    "StoreNotFoundError",
    "StoreTransitionError",
    "SubmissionResult",
    "validate_comparison_inputs",
    "validate_execution_contract",
    "validate_run_result",
]
