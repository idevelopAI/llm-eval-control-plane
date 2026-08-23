"""Durable control-plane use cases over an injected metadata repository."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from llm_eval_control_plane.application.comparison import compare_runs
from llm_eval_control_plane.domain.artifacts import ArtifactKind, ArtifactRef
from llm_eval_control_plane.domain.canonical import JsonValue, sha256_digest
from llm_eval_control_plane.domain.comparison import ReleaseStatus
from llm_eval_control_plane.domain.control_plane import (
    CursorPage,
    DatasetListRecord,
    DatasetRecord,
    JobKind,
    JobRecord,
    JobStatus,
    ReleaseDecisionListRecord,
    ReleaseDecisionRecord,
    RunListRecord,
    RunRecord,
)
from llm_eval_control_plane.domain.datasets import DatasetVersion
from llm_eval_control_plane.domain.evaluation import EvaluationSpec
from llm_eval_control_plane.domain.results import ExecutionMode, RunResult


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

    def begin_job(self, record: JobRecord) -> tuple[JobRecord, bool]: ...

    def get_job(self, job_id: str) -> JobRecord: ...

    def list_jobs(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        kind: JobKind | None = None,
        status: JobStatus | None = None,
    ) -> CursorPage[JobRecord]: ...

    def transition_job(
        self,
        job_id: str,
        status: JobStatus,
        *,
        at: datetime,
        error_code: str | None = None,
    ) -> JobRecord: ...

    def complete_run(
        self,
        job_id: str,
        record: RunRecord,
        *,
        at: datetime,
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
        at: datetime,
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


@dataclass(frozen=True, slots=True)
class ExecutionContract:
    """Resolved evidence identities promised by an execution adapter."""

    adapter: str
    evaluator_names: tuple[str, ...]
    target: ArtifactRef
    evaluators: tuple[ArtifactRef, ...]
    execution_mode: ExecutionMode

    def __post_init__(self) -> None:
        if not self.adapter or not self.evaluator_names:
            raise ValueError("execution contract identity is incomplete")
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
    """Coordinate immutable metadata, synchronous execution, and release gates."""

    def __init__(
        self,
        *,
        repository: ControlPlaneRepository,
        executor: EvaluationExecutor,
        clock: Clock = _utc_now,
        identifier_factory: IdentifierFactory = _identifier,
    ) -> None:
        self._repository = repository
        self._executor = executor
        self._clock = clock
        self._identifier_factory = identifier_factory

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
        """Claim one idempotent job and execute it only for the insert winner."""
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
            self._validate_execution_contract(execution_contract, submission)
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
            created_at=now,
            updated_at=now,
        )
        try:
            job, created = self._repository.begin_job(proposed)
        except StoreIdempotencyConflictError as error:
            raise IdempotencyConflictError(
                "Idempotency key was used for a different request"
            ) from error
        except StoreConflictError as error:
            raise ResourceConflictError("Job identity already exists") from error
        if not created:
            return SubmissionResult(job=job, created=False)

        running = self._repository.transition_job(
            job.job_id,
            JobStatus.RUNNING,
            at=self._clock(),
        )
        try:
            result = await self._executor.execute(
                run_id=running.resource_id,
                dataset=dataset_record.dataset,
                target_name=submission.target_name,
                target_revision=submission.target_revision,
                adapter=submission.adapter,
                evaluator_names=submission.evaluator_names,
                scenario_overrides=submission.scenario_overrides,
            )
            self._validate_run(
                result,
                running,
                dataset_record,
                execution_contract,
            )
        except Exception:
            return SubmissionResult(
                job=self._fail_job(running, "execution_failed"),
                created=True,
            )

        record = RunRecord(result=result, created_at=self._clock())
        try:
            completed = self._repository.complete_run(
                running.job_id,
                record,
                at=self._clock(),
            )
        except StoreConflictError:
            return SubmissionResult(
                job=self._fail_job(running, "evidence_conflict"),
                created=True,
            )
        return SubmissionResult(job=completed, created=True)

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
        """Claim and compute one idempotent release decision."""
        try:
            dataset = self._repository.get_dataset(
                submission.dataset_name,
                submission.dataset_revision,
            )
            baseline = self._repository.get_run(submission.baseline_run_id)
            candidate = self._repository.get_run(submission.candidate_run_id)
            self._validate_comparison_submission(
                submission,
                dataset=dataset,
                baseline=baseline,
                candidate=candidate,
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
            created_at=now,
            updated_at=now,
        )
        try:
            job, created = self._repository.begin_job(proposed)
        except StoreIdempotencyConflictError as error:
            raise IdempotencyConflictError(
                "Idempotency key was used for a different request"
            ) from error
        except StoreConflictError as error:
            raise ResourceConflictError("Job identity already exists") from error
        if not created:
            return SubmissionResult(job=job, created=False)

        running = self._repository.transition_job(
            job.job_id,
            JobStatus.RUNNING,
            at=self._clock(),
        )
        try:
            decision = compare_runs(
                spec=submission.spec,
                dataset=dataset.dataset,
                baseline=baseline.result,
                candidate=candidate.result,
            )
        except Exception:
            return SubmissionResult(
                job=self._fail_job(running, "comparison_failed"),
                created=True,
            )

        record = ReleaseDecisionRecord(
            decision_id=running.resource_id,
            decision=decision,
            created_at=self._clock(),
        )
        try:
            completed = self._repository.complete_release_decision(
                running.job_id,
                record,
                at=self._clock(),
            )
        except StoreConflictError:
            return SubmissionResult(
                job=self._fail_job(running, "evidence_conflict"),
                created=True,
            )
        return SubmissionResult(job=completed, created=True)

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

    def _fail_job(self, job: JobRecord, error_code: str) -> JobRecord:
        return self._repository.transition_job(
            job.job_id,
            JobStatus.FAILED,
            at=self._clock(),
            error_code=error_code,
        )

    @staticmethod
    def _validate_run(
        result: RunResult,
        job: JobRecord,
        dataset: DatasetRecord,
        contract: ExecutionContract,
    ) -> None:
        if result.run_id != job.resource_id:
            raise ValueError("executor returned an unexpected run identity")
        if result.dataset != dataset.dataset.artifact_ref:
            raise ValueError("executor returned an unexpected dataset identity")
        if result.target != contract.target:
            raise ValueError("executor returned an unexpected target identity")
        if result.evaluators != contract.evaluators:
            raise ValueError("executor returned unexpected evaluator identities")
        allowed_evaluators = set(contract.evaluators)
        if any(
            summary.evaluator not in allowed_evaluators for summary in result.metrics
        ):
            raise ValueError("executor returned an unexpected metric evaluator")
        if any(
            observation.evaluator not in allowed_evaluators
            for case in result.cases
            for observation in case.observations
        ):
            raise ValueError("executor returned an unexpected observation evaluator")
        if any(
            failure.evaluator is not None
            and failure.evaluator not in allowed_evaluators
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

    @staticmethod
    def _validate_execution_contract(
        contract: ExecutionContract,
        submission: RunSubmission,
    ) -> None:
        if contract.adapter != submission.adapter:
            raise ValueError("executor resolved a different adapter")
        if contract.evaluator_names != submission.evaluator_names:
            raise ValueError("executor resolved different evaluators")
        if (
            contract.target.name != submission.target_name
            or contract.target.revision != submission.target_revision
        ):
            raise ValueError("executor resolved a different target")

    @staticmethod
    def _validate_comparison_submission(
        submission: ComparisonSubmission,
        *,
        dataset: DatasetRecord,
        baseline: RunRecord,
        candidate: RunRecord,
    ) -> None:
        spec = submission.spec
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
        if spec.dataset != dataset.dataset.artifact_ref:
            raise ValueError("spec references a different dataset revision")
        if baseline.result.run_id != submission.baseline_run_id:
            raise ValueError("baseline run identity does not match")
        if candidate.result.run_id != submission.candidate_run_id:
            raise ValueError("candidate run identity does not match")
        if spec.baseline != baseline.result.target:
            raise ValueError("spec baseline does not match baseline run")
        if spec.candidate != candidate.result.target:
            raise ValueError("spec candidate does not match candidate run")

    @staticmethod
    def _validate_dataset_bounds(dataset: DatasetVersion) -> None:
        if len(dataset.cases) > _MAX_DATASET_CASES:
            raise ValueError("dataset contains too many cases")
        if any(len(case.slices) > _MAX_SLICES_PER_CASE for case in dataset.cases):
            raise ValueError("dataset case contains too many slices")
        slices = {label for case in dataset.cases for label in case.slices}
        if len(slices) > _MAX_DATASET_SLICES:
            raise ValueError("dataset contains too many unique slices")


__all__ = [
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
    "StoreNotFoundError",
    "StoreTransitionError",
    "SubmissionResult",
]
