"""Single-attempt leased worker orchestration with fenced publication."""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from llm_eval_control_plane.application.comparison import compare_runs
from llm_eval_control_plane.application.control_plane import (
    ClaimedJob,
    ControlPlaneRepository,
    ControlPlaneStoreError,
    EvaluationExecutor,
    ExecutionContract,
    StoreConflictError,
    StoreLeaseLostError,
    StoreNotFoundError,
    validate_comparison_inputs,
    validate_execution_contract,
    validate_run_result,
)
from llm_eval_control_plane.domain.comparison import ReleaseDecision
from llm_eval_control_plane.domain.control_plane import (
    ComparisonJobPayload,
    DatasetRecord,
    JobAttemptStatus,
    JobKind,
    JobStatus,
    ReleaseDecisionRecord,
    RunJobPayload,
    RunRecord,
)

Clock = Callable[[], datetime]
LeaseTokenFactory = Callable[[], str]
Sleeper = Callable[[float], Awaitable[None]]
TraceJob = Callable[[JobKind, str | None], AbstractContextManager[None]]

_PRIVATE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LEASE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _lease_token() -> str:
    return secrets.token_urlsafe(32)


@contextmanager
def _noop_trace_job(
    _kind: JobKind,
    _traceparent: str | None,
) -> Iterator[None]:
    yield


class WorkerError(RuntimeError):
    """Base content-safe worker failure."""


class WorkerConfigurationError(WorkerError):
    """Raised when private worker controls violate their bounded contract."""

    def __init__(self) -> None:
        super().__init__("Worker configuration is invalid")


class WorkerUnavailableError(WorkerError):
    """Raised when durable coordination cannot be completed safely."""

    def __init__(self) -> None:
        super().__init__("Worker persistence is unavailable")


class WorkerInvariantError(WorkerError):
    """Raised when a repository returns an internally inconsistent claim."""

    def __init__(self) -> None:
        super().__init__("Worker claim is invalid")


class TransientExecutionError(WorkerError):
    """Explicit opt-in signal for a safely retryable whole-job failure."""

    def __init__(self) -> None:
        super().__init__("Worker execution failed transiently")


class WorkerResultStatus(StrEnum):
    """Content-safe outcome of one bounded worker service call."""

    IDLE = "idle"
    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    CANCELED = "canceled"
    LEASE_LOST = "lease_lost"


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """Minimal result that never retains a payload, token, or worker identity."""

    status: WorkerResultStatus
    job_id: str | None = None
    attempt_number: int | None = None


class _HeartbeatSignal(StrEnum):
    CONTINUE = "continue"
    CANCEL = "cancel"
    LEASE_LOST = "lease_lost"
    UNAVAILABLE = "unavailable"


Evidence = RunRecord | ReleaseDecisionRecord


class WorkerService:
    """Claim and process at most one job using a private fencing token."""

    __slots__ = (
        "_backoff_base_seconds",
        "_backoff_max_seconds",
        "_clock",
        "_executor",
        "_heartbeat_seconds",
        "_lease_seconds",
        "_lease_token_factory",
        "_repository",
        "_sleeper",
        "_trace_job",
        "_worker_id",
    )

    def __init__(
        self,
        *,
        repository: ControlPlaneRepository,
        executor: EvaluationExecutor,
        worker_id: str,
        lease_seconds: int = 30,
        heartbeat_seconds: int = 10,
        backoff_base_seconds: int = 1,
        backoff_max_seconds: int = 60,
        clock: Clock = _utc_now,
        lease_token_factory: LeaseTokenFactory = _lease_token,
        sleeper: Sleeper = asyncio.sleep,
        trace_job: TraceJob = _noop_trace_job,
    ) -> None:
        integers = (
            (lease_seconds, 5, 3_600),
            (heartbeat_seconds, 1, 1_800),
            (backoff_base_seconds, 1, 300),
            (backoff_max_seconds, 1, 3_600),
        )
        if (
            not isinstance(worker_id, str)
            or _PRIVATE_ID_PATTERN.fullmatch(worker_id) is None
            or any(
                type(value) is not int or not lower <= value <= upper
                for value, lower, upper in integers
            )
            or heartbeat_seconds * 2 > lease_seconds
            or backoff_max_seconds < backoff_base_seconds
        ):
            raise WorkerConfigurationError
        self._repository = repository
        self._executor = executor
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._clock = clock
        self._lease_token_factory = lease_token_factory
        self._sleeper = sleeper
        self._trace_job = trace_job

    def __repr__(self) -> str:
        return "WorkerService()"

    async def run_once(self) -> WorkerResult:
        """Claim and process no more than one available durable job."""
        try:
            lease_token = self._lease_token_factory()
        except Exception:
            raise WorkerConfigurationError from None
        if (
            not isinstance(lease_token, str)
            or _LEASE_TOKEN_PATTERN.fullmatch(lease_token) is None
        ):
            raise WorkerConfigurationError
        try:
            claim = self._repository.claim_next_job(
                worker_id=self._worker_id,
                lease_token=lease_token,
                lease_seconds=self._lease_seconds,
            )
        except ControlPlaneStoreError:
            raise WorkerUnavailableError from None
        if claim is None:
            return WorkerResult(status=WorkerResultStatus.IDLE)
        self._validate_claim(claim, lease_token)
        with self._trace_job(claim.job.kind, claim.job.traceparent):
            if claim.job.kind is not claim.payload.kind:
                return self._fail(claim, error_code="invalid_job_payload")
            return await self._process(claim)

    async def _process(self, claim: ClaimedJob) -> WorkerResult:
        execution = asyncio.create_task(self._build_evidence(claim))
        monitor = asyncio.create_task(self._monitor_heartbeat(claim))
        try:
            done, _pending = await asyncio.wait(
                (execution, monitor),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if monitor in done:
                signal = monitor.result()
                await self._stop_task(execution)
                return self._handle_heartbeat_signal(claim, signal)

            await self._stop_task(monitor)
            signal = self._heartbeat(claim)
            if signal is not _HeartbeatSignal.CONTINUE:
                return self._handle_heartbeat_signal(claim, signal)
            try:
                evidence = execution.result()
            except TransientExecutionError:
                return self._retry_or_exhaust(claim)
            except StoreNotFoundError:
                return self._fail(claim, error_code="dependency_not_found")
            except ControlPlaneStoreError:
                raise WorkerUnavailableError from None
            except ValueError:
                return self._fail(claim, error_code="invalid_job_payload")
            except Exception:
                error_code = (
                    "comparison_failed"
                    if isinstance(claim.payload, ComparisonJobPayload)
                    else "execution_failed"
                )
                return self._fail(claim, error_code=error_code)
            return self._publish(claim, evidence)
        finally:
            await self._stop_task(monitor)
            await self._stop_task(execution)

    async def _build_evidence(self, claim: ClaimedJob) -> Evidence:
        payload = claim.payload
        if isinstance(payload, RunJobPayload):
            dataset = await asyncio.to_thread(
                _load_run_dataset,
                repository=self._repository,
                payload=payload,
            )
            scenario_overrides = {
                item.case_id: item.scenario for item in payload.scenario_overrides
            }
            contract = await asyncio.to_thread(
                _resolve_run_contract,
                executor=self._executor,
                payload=payload,
                scenario_overrides=scenario_overrides,
            )
            result = await self._executor.execute(
                run_id=claim.job.resource_id,
                dataset=dataset.dataset,
                target_name=payload.target_name,
                target_revision=payload.target_revision,
                adapter=payload.adapter,
                evaluator_names=payload.evaluator_names,
                scenario_overrides=scenario_overrides,
            )
            await asyncio.to_thread(
                validate_run_result,
                result,
                resource_id=claim.job.resource_id,
                dataset=dataset,
                contract=contract,
            )
            return RunRecord(result=result, created_at=self._now())

        if not isinstance(payload, ComparisonJobPayload):
            raise ValueError("worker payload kind is unsupported")
        decision = await asyncio.to_thread(
            _compare_job,
            repository=self._repository,
            payload=payload,
        )
        return ReleaseDecisionRecord(
            decision_id=claim.job.resource_id,
            decision=decision,
            created_at=self._now(),
        )

    async def _monitor_heartbeat(self, claim: ClaimedJob) -> _HeartbeatSignal:
        while True:
            try:
                await self._sleeper(float(self._heartbeat_seconds))
            except asyncio.CancelledError:
                raise
            except Exception:
                return _HeartbeatSignal.UNAVAILABLE
            signal = self._heartbeat(claim)
            if signal is not _HeartbeatSignal.CONTINUE:
                return signal

    def _heartbeat(self, claim: ClaimedJob) -> _HeartbeatSignal:
        try:
            job = self._repository.heartbeat_job(
                claim.job.job_id,
                claim.attempt.attempt_number,
                claim.lease_token,
                lease_seconds=self._lease_seconds,
            )
        except StoreLeaseLostError:
            return _HeartbeatSignal.LEASE_LOST
        except ControlPlaneStoreError:
            return _HeartbeatSignal.UNAVAILABLE
        if job.status is JobStatus.CANCEL_REQUESTED:
            return _HeartbeatSignal.CANCEL
        if job.status is not JobStatus.RUNNING:
            return _HeartbeatSignal.LEASE_LOST
        return _HeartbeatSignal.CONTINUE

    def _handle_heartbeat_signal(
        self,
        claim: ClaimedJob,
        signal: _HeartbeatSignal,
    ) -> WorkerResult:
        if signal is _HeartbeatSignal.CANCEL:
            try:
                canceled = self._repository.acknowledge_cancellation(
                    claim.job.job_id,
                    claim.attempt.attempt_number,
                    claim.lease_token,
                )
            except StoreLeaseLostError:
                return self._result(claim, WorkerResultStatus.LEASE_LOST)
            except ControlPlaneStoreError:
                raise WorkerUnavailableError from None
            if canceled.status is not JobStatus.CANCELED:
                raise WorkerInvariantError
            return self._result(claim, WorkerResultStatus.CANCELED)
        if signal is _HeartbeatSignal.LEASE_LOST:
            return self._result(claim, WorkerResultStatus.LEASE_LOST)
        if signal is _HeartbeatSignal.UNAVAILABLE:
            raise WorkerUnavailableError from None
        raise WorkerInvariantError

    def _publish(self, claim: ClaimedJob, evidence: Evidence) -> WorkerResult:
        try:
            if isinstance(evidence, RunRecord):
                completed = self._repository.complete_run(
                    claim.job.job_id,
                    evidence,
                    attempt_number=claim.attempt.attempt_number,
                    lease_token=claim.lease_token,
                )
            else:
                completed = self._repository.complete_release_decision(
                    claim.job.job_id,
                    evidence,
                    attempt_number=claim.attempt.attempt_number,
                    lease_token=claim.lease_token,
                )
        except StoreLeaseLostError:
            return self._result(claim, WorkerResultStatus.LEASE_LOST)
        except StoreConflictError:
            return self._fail(claim, error_code="evidence_conflict")
        except ControlPlaneStoreError:
            raise WorkerUnavailableError from None
        if completed.status is JobStatus.CANCELED:
            return self._result(claim, WorkerResultStatus.CANCELED)
        if completed.status is not JobStatus.SUCCEEDED:
            raise WorkerInvariantError
        return self._result(claim, WorkerResultStatus.SUCCEEDED)

    def _retry_or_exhaust(self, claim: ClaimedJob) -> WorkerResult:
        if claim.job.attempt_count >= claim.job.max_attempts:
            return self._fail(claim, error_code="retry_exhausted")
        delay_seconds = min(
            self._backoff_base_seconds * 2 ** (claim.job.attempt_count - 1),
            self._backoff_max_seconds,
        )
        try:
            retried = self._repository.retry_job(
                claim.job.job_id,
                claim.attempt.attempt_number,
                claim.lease_token,
                delay_seconds=delay_seconds,
                error_code="transient_execution_failure",
            )
        except StoreLeaseLostError:
            return self._result(claim, WorkerResultStatus.LEASE_LOST)
        except ControlPlaneStoreError:
            raise WorkerUnavailableError from None
        if retried.status is JobStatus.CANCELED:
            return self._result(claim, WorkerResultStatus.CANCELED)
        if retried.status is not JobStatus.QUEUED:
            raise WorkerInvariantError
        return self._result(claim, WorkerResultStatus.RETRY_SCHEDULED)

    def _fail(self, claim: ClaimedJob, *, error_code: str) -> WorkerResult:
        try:
            failed = self._repository.fail_job(
                claim.job.job_id,
                claim.attempt.attempt_number,
                claim.lease_token,
                error_code=error_code,
            )
        except StoreLeaseLostError:
            return self._result(claim, WorkerResultStatus.LEASE_LOST)
        except ControlPlaneStoreError:
            raise WorkerUnavailableError from None
        if failed.status is JobStatus.CANCELED:
            return self._result(claim, WorkerResultStatus.CANCELED)
        if failed.status is not JobStatus.FAILED:
            raise WorkerInvariantError
        return self._result(claim, WorkerResultStatus.FAILED)

    @staticmethod
    def _result(claim: ClaimedJob, status: WorkerResultStatus) -> WorkerResult:
        return WorkerResult(
            status=status,
            job_id=claim.job.job_id,
            attempt_number=claim.attempt.attempt_number,
        )

    def _validate_claim(self, claim: ClaimedJob, lease_token: str) -> None:
        if (
            claim.lease_token != lease_token
            or claim.job.status is not JobStatus.RUNNING
            or claim.attempt.status is not JobAttemptStatus.RUNNING
            or claim.attempt.job_id != claim.job.job_id
            or claim.attempt.attempt_number != claim.job.attempt_count
        ):
            raise WorkerInvariantError

    def _now(self) -> datetime:
        try:
            value = self._clock()
        except Exception:
            raise WorkerConfigurationError from None
        if value.tzinfo is None or value.utcoffset() is None:
            raise WorkerConfigurationError
        return value.astimezone(UTC)

    @staticmethod
    async def _stop_task(task: asyncio.Task[object]) -> None:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: S110 -- cleanup must not mask the job outcome
            pass


def _load_run_dataset(
    *,
    repository: ControlPlaneRepository,
    payload: RunJobPayload,
) -> DatasetRecord:
    """Load and verify pinned run input away from the heartbeat event loop."""
    dataset = repository.get_dataset(
        payload.dataset.name,
        payload.dataset.revision,
    )
    if dataset.dataset.artifact_ref != payload.dataset:
        raise ValueError("stored dataset does not match the worker payload")
    return dataset


def _resolve_run_contract(
    *,
    executor: EvaluationExecutor,
    payload: RunJobPayload,
    scenario_overrides: dict[str, str],
) -> ExecutionContract:
    """Resolve and verify synchronous executor metadata off the heartbeat loop."""
    contract = executor.validate(
        target_name=payload.target_name,
        target_revision=payload.target_revision,
        adapter=payload.adapter,
        evaluator_names=payload.evaluator_names,
        scenario_overrides=scenario_overrides,
    )
    validate_execution_contract(
        contract,
        target_name=payload.target_name,
        target_revision=payload.target_revision,
        adapter=payload.adapter,
        evaluator_names=payload.evaluator_names,
    )
    if contract != payload.execution_contract:
        raise ValueError("executor contract changed after submission")
    return contract


def _compare_job(
    *,
    repository: ControlPlaneRepository,
    payload: ComparisonJobPayload,
) -> ReleaseDecision:
    """Load, validate, and compare evidence without starving lease heartbeats."""
    dataset = repository.get_dataset(
        payload.dataset.name,
        payload.dataset.revision,
    )
    baseline = repository.get_run(payload.baseline_run_id)
    candidate = repository.get_run(payload.candidate_run_id)
    if (
        baseline.result.result_digest != payload.baseline_result_digest
        or candidate.result.result_digest != payload.candidate_result_digest
    ):
        raise ValueError("comparison inputs changed after submission")
    validate_comparison_inputs(
        dataset_name=payload.dataset.name,
        dataset_revision=payload.dataset.revision,
        baseline_run_id=payload.baseline_run_id,
        candidate_run_id=payload.candidate_run_id,
        spec=payload.spec,
        dataset=dataset,
        baseline=baseline,
        candidate=candidate,
    )
    return compare_runs(
        spec=payload.spec,
        dataset=dataset.dataset,
        baseline=baseline.result,
        candidate=candidate.result,
    )


__all__ = [
    "TraceJob",
    "TransientExecutionError",
    "WorkerConfigurationError",
    "WorkerError",
    "WorkerInvariantError",
    "WorkerResult",
    "WorkerResultStatus",
    "WorkerService",
    "WorkerUnavailableError",
]
