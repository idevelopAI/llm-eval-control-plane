from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import cast

from pytest import MonkeyPatch, raises

from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.application.comparison import (
    compare_runs as compare_runs_sync,
)
from llm_eval_control_plane.application.control_plane import (
    ClaimedJob,
    ControlPlaneRepository,
    ControlPlaneStoreError,
    StoreConflictError,
    StoreLeaseLostError,
    StoreNotFoundError,
)
from llm_eval_control_plane.application.worker import (
    TransientExecutionError,
    WorkerConfigurationError,
    WorkerInvariantError,
    WorkerResultStatus,
    WorkerService,
    WorkerUnavailableError,
)
from llm_eval_control_plane.domain import (
    CanonicalJson,
    DatasetVersion,
    EvaluationCase,
    EvaluationSpec,
    MetricDirection,
    MetricGate,
    ReleaseDecision,
    RunResult,
    sha256_digest,
)
from llm_eval_control_plane.domain.control_plane import (
    ComparisonJobPayload,
    DatasetRecord,
    ExecutionContract,
    JobAttemptRecord,
    JobAttemptStatus,
    JobKind,
    JobPayload,
    JobRecord,
    JobStatus,
    ReleaseDecisionRecord,
    RunJobPayload,
    RunRecord,
)

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _dataset() -> DatasetVersion:
    return DatasetVersion.create(
        name="worker/fixture",
        revision=1,
        cases=(
            EvaluationCase(
                case_id="case-001",
                input=CanonicalJson.from_value(
                    {"scenario": "echo", "value": "private-payload-sentinel"}
                ),
                expected=CanonicalJson.from_value("private-payload-sentinel"),
            ),
        ),
    )


def _job(
    *,
    kind: JobKind = JobKind.RUN,
    resource_id: str = "run-worker-001",
    attempt_count: int = 1,
    max_attempts: int = 3,
    traceparent: str | None = None,
) -> JobRecord:
    return JobRecord(
        job_id="job-worker-001",
        kind=kind,
        status=JobStatus.RUNNING,
        idempotency_key="private-idempotency-key",
        request_digest=sha256_digest({"request": "worker"}),
        resource_id=resource_id,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        available_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        traceparent=traceparent,
    )


class CountingExecutor(DeterministicEvaluationExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

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
        self.calls += 1
        return await super().execute(
            run_id=run_id,
            dataset=dataset,
            target_name=target_name,
            target_revision=target_revision,
            adapter=adapter,
            evaluator_names=evaluator_names,
            scenario_overrides=scenario_overrides,
        )


class FailingExecutor(CountingExecutor):
    def __init__(self, *, transient: bool) -> None:
        super().__init__()
        self.transient = transient

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
        del (
            run_id,
            dataset,
            target_name,
            target_revision,
            adapter,
            evaluator_names,
            scenario_overrides,
        )
        self.calls += 1
        if self.transient:
            raise TransientExecutionError
        raise RuntimeError(
            "private-payload-sentinel private-token private-worker-identity"
        )


class BlockingExecutor(CountingExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

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
        self.started.set()
        await self.release.wait()
        return await super().execute(
            run_id=run_id,
            dataset=dataset,
            target_name=target_name,
            target_revision=target_revision,
            adapter=adapter,
            evaluator_names=evaluator_names,
            scenario_overrides=scenario_overrides,
        )


class ManualSleeper:
    def __init__(self) -> None:
        self.tick = asyncio.Event()
        self.calls = 0

    async def __call__(self, _seconds: float) -> None:
        await self.tick.wait()
        self.tick.clear()
        self.calls += 1


class FakeRepository:
    def __init__(
        self,
        *,
        job: JobRecord,
        payload: JobPayload,
        dataset: DatasetVersion,
    ) -> None:
        self.job = job
        self.payload = payload
        self.dataset = DatasetRecord(dataset=dataset, created_at=NOW)
        self.runs: dict[str, RunRecord] = {}
        self.claim = True
        self.claim_error: ControlPlaneStoreError | None = None
        self.claim_tokens: list[str] = []
        self.claim_worker_ids: list[str] = []
        self.claim_lease_seconds: list[int] = []
        self.heartbeat_calls = 0
        self.heartbeat_lease_seconds: list[int] = []
        self.heartbeat_status = JobStatus.RUNNING
        self.heartbeat_error: ControlPlaneStoreError | None = None
        self.heartbeat_seen = asyncio.Event()
        self.block_dataset_load = False
        self.dataset_load_started = Event()
        self.dataset_load_release = Event()
        self.completed_run: RunRecord | None = None
        self.completed_decision: ReleaseDecisionRecord | None = None
        self.completion_error: ControlPlaneStoreError | None = None
        self.completion_status: JobStatus | None = None
        self.retry_call: tuple[int, str] | None = None
        self.retry_error: ControlPlaneStoreError | None = None
        self.retry_status: JobStatus | None = None
        self.fail_code: str | None = None
        self.fail_error: ControlPlaneStoreError | None = None
        self.fail_status: JobStatus | None = None
        self.acknowledged = False
        self.acknowledge_error: ControlPlaneStoreError | None = None
        self.acknowledge_status: JobStatus | None = None
        self.cancel_during_mutation = False

    def claim_next_job(
        self,
        *,
        worker_id: str,
        lease_token: str,
        lease_seconds: int,
    ) -> ClaimedJob | None:
        if self.claim_error is not None:
            raise self.claim_error
        self.claim_tokens.append(lease_token)
        self.claim_worker_ids.append(worker_id)
        self.claim_lease_seconds.append(lease_seconds)
        if not self.claim:
            return None
        attempt = JobAttemptRecord(
            job_id=self.job.job_id,
            attempt_number=self.job.attempt_count,
            status=JobAttemptStatus.RUNNING,
            started_at=NOW,
            heartbeat_at=NOW,
            lease_expires_at=NOW + timedelta(seconds=lease_seconds),
        )
        return ClaimedJob(
            job=self.job,
            payload=self.payload,
            attempt=attempt,
            lease_token=lease_token,
        )

    def heartbeat_job(
        self,
        _job_id: str,
        _attempt_number: int,
        _lease_token: str,
        *,
        lease_seconds: int,
    ) -> JobRecord:
        self.heartbeat_lease_seconds.append(lease_seconds)
        self.heartbeat_calls += 1
        self.heartbeat_seen.set()
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        if self.heartbeat_status is JobStatus.CANCEL_REQUESTED:
            self.job = self.job.transition_to(JobStatus.CANCEL_REQUESTED, at=NOW)
        elif self.heartbeat_status is not JobStatus.RUNNING:
            return self.job.model_copy(update={"status": self.heartbeat_status})
        return self.job

    def get_dataset(self, name: str, revision: int) -> DatasetRecord:
        if self.block_dataset_load:
            self.dataset_load_started.set()
            if not self.dataset_load_release.wait(timeout=10):
                raise AssertionError("evidence load blocked the worker event loop")
        if (name, revision) != (
            self.dataset.dataset.name,
            self.dataset.dataset.revision,
        ):
            raise StoreNotFoundError("private missing dataset")
        return self.dataset

    def get_run(self, run_id: str) -> RunRecord:
        try:
            return self.runs[run_id]
        except KeyError:
            raise StoreNotFoundError("private missing run") from None

    def retry_job(
        self,
        _job_id: str,
        _attempt_number: int,
        _lease_token: str,
        *,
        delay_seconds: int,
        error_code: str,
    ) -> JobRecord:
        if self.retry_error is not None:
            raise self.retry_error
        if self.cancel_during_mutation:
            return self._cancel()
        self.retry_call = (delay_seconds, error_code)
        self.job = self.job.transition_to(
            JobStatus.QUEUED,
            at=NOW,
            available_at=NOW + timedelta(seconds=delay_seconds),
        )
        if self.retry_status is not None:
            return self.job.model_copy(update={"status": self.retry_status})
        return self.job

    def fail_job(
        self,
        _job_id: str,
        _attempt_number: int,
        _lease_token: str,
        *,
        error_code: str,
    ) -> JobRecord:
        if self.fail_error is not None:
            raise self.fail_error
        if self.cancel_during_mutation:
            return self._cancel()
        self.fail_code = error_code
        self.job = self.job.transition_to(
            JobStatus.FAILED,
            at=NOW,
            error_code=error_code,
        )
        if self.fail_status is not None:
            return self.job.model_copy(update={"status": self.fail_status})
        return self.job

    def acknowledge_cancellation(
        self,
        _job_id: str,
        _attempt_number: int,
        _lease_token: str,
    ) -> JobRecord:
        if self.acknowledge_error is not None:
            raise self.acknowledge_error
        self.acknowledged = True
        self.job = self.job.transition_to(JobStatus.CANCELED, at=NOW)
        if self.acknowledge_status is not None:
            return self.job.model_copy(update={"status": self.acknowledge_status})
        return self.job

    def complete_run(
        self,
        _job_id: str,
        record: RunRecord,
        *,
        attempt_number: int,
        lease_token: str,
    ) -> JobRecord:
        del attempt_number, lease_token
        if self.completion_error is not None:
            raise self.completion_error
        if self.cancel_during_mutation:
            return self._cancel()
        self.completed_run = record
        self.job = self.job.transition_to(JobStatus.SUCCEEDED, at=NOW)
        if self.completion_status is not None:
            return self.job.model_copy(update={"status": self.completion_status})
        return self.job

    def complete_release_decision(
        self,
        _job_id: str,
        record: ReleaseDecisionRecord,
        *,
        attempt_number: int,
        lease_token: str,
    ) -> JobRecord:
        del attempt_number, lease_token
        if self.completion_error is not None:
            raise self.completion_error
        if self.cancel_during_mutation:
            return self._cancel()
        self.completed_decision = record
        self.job = self.job.transition_to(JobStatus.SUCCEEDED, at=NOW)
        if self.completion_status is not None:
            return self.job.model_copy(update={"status": self.completion_status})
        return self.job

    def _cancel(self) -> JobRecord:
        self.job = self.job.transition_to(JobStatus.CANCEL_REQUESTED, at=NOW)
        self.job = self.job.transition_to(JobStatus.CANCELED, at=NOW)
        return self.job


def _run_payload(
    dataset: DatasetVersion,
    executor: DeterministicEvaluationExecutor,
) -> RunJobPayload:
    contract = executor.validate(
        target_name="fake/worker",
        target_revision=1,
        adapter="deterministic_fake",
        evaluator_names=("exact_match",),
        scenario_overrides={},
    )
    return RunJobPayload(
        dataset=dataset.artifact_ref,
        target_name="fake/worker",
        target_revision=1,
        adapter="deterministic_fake",
        evaluator_names=("exact_match",),
        execution_contract=contract,
    )


def _service(
    repository: FakeRepository,
    executor: DeterministicEvaluationExecutor,
    **overrides: object,
) -> WorkerService:
    arguments: dict[str, object] = {
        "repository": cast(ControlPlaneRepository, repository),
        "executor": executor,
        "worker_id": "private-worker-identity",
        "clock": lambda: NOW,
        "lease_token_factory": lambda: "privateLeaseToken_1234567890abcdef",
    }
    arguments.update(overrides)
    return WorkerService(**arguments)  # type: ignore[arg-type]


def _run_context(
    *,
    executor: CountingExecutor | None = None,
    attempt_count: int = 1,
    max_attempts: int = 3,
) -> tuple[FakeRepository, CountingExecutor, WorkerService]:
    selected = CountingExecutor() if executor is None else executor
    dataset = _dataset()
    repository = FakeRepository(
        job=_job(attempt_count=attempt_count, max_attempts=max_attempts),
        payload=_run_payload(dataset, selected),
        dataset=dataset,
    )
    return repository, selected, _service(repository, selected)


def _comparison_context() -> tuple[FakeRepository, CountingExecutor, WorkerService]:
    executor = CountingExecutor()
    dataset = _dataset()
    baseline = asyncio.run(
        executor.execute(
            run_id="baseline-worker",
            dataset=dataset,
            target_name="fake/baseline",
            target_revision=1,
            adapter="deterministic_fake",
            evaluator_names=("exact_match",),
            scenario_overrides={},
        )
    )
    candidate = asyncio.run(
        executor.execute(
            run_id="candidate-worker",
            dataset=dataset,
            target_name="fake/candidate",
            target_revision=2,
            adapter="deterministic_fake",
            evaluator_names=("exact_match",),
            scenario_overrides={},
        )
    )
    spec = EvaluationSpec(
        name="worker-release-policy",
        dataset=dataset.artifact_ref,
        baseline=baseline.target,
        candidate=candidate.target,
        gates=(
            MetricGate(
                metric="quality.exact_match",
                direction=MetricDirection.HIGHER_IS_BETTER,
                threshold=1.0,
            ),
        ),
    )
    payload = ComparisonJobPayload(
        dataset=dataset.artifact_ref,
        baseline_run_id=baseline.run_id,
        baseline_result_digest=baseline.result_digest,
        candidate_run_id=candidate.run_id,
        candidate_result_digest=candidate.result_digest,
        spec=spec,
    )
    repository = FakeRepository(
        job=_job(kind=JobKind.COMPARISON, resource_id="decision-worker-001"),
        payload=payload,
        dataset=dataset,
    )
    repository.runs = {
        baseline.run_id: RunRecord(result=baseline, created_at=NOW),
        candidate.run_id: RunRecord(result=candidate, created_at=NOW),
    }
    return repository, executor, _service(repository, executor)


def test_run_attempt_claims_with_private_fence_and_publishes_once() -> None:
    repository, executor, service = _run_context()

    result = asyncio.run(service.run_once())

    assert result.status is WorkerResultStatus.SUCCEEDED
    assert executor.calls == 1
    assert repository.completed_run is not None
    assert repository.completed_run.run_id == "run-worker-001"
    assert repository.heartbeat_calls == 1
    assert repository.claim_lease_seconds == [30]
    assert repository.heartbeat_lease_seconds == [30]
    assert len(repository.claim_tokens[0]) >= 32
    assert "privateLeaseToken" not in repr(result)
    assert "private-worker-identity" not in repr(service)
    assert "private-payload-sentinel" not in repr(result)


def test_claim_repr_never_exposes_private_worker_state() -> None:
    repository, _executor, _service_instance = _run_context()
    claim = repository.claim_next_job(
        worker_id="private-worker-identity",
        lease_token="privateLeaseToken_1234567890abcdef",
        lease_seconds=30,
    )

    assert claim is not None
    assert f"{claim!r} {claim!s}" == "ClaimedJob() ClaimedJob()"
    for private_value in (
        "job-worker-001",
        "private-idempotency-key",
        claim.job.request_digest,
        "private-payload-sentinel",
        "private-worker-identity",
        "privateLeaseToken_1234567890abcdef",
    ):
        assert private_value not in repr(claim)


def test_comparison_attempt_uses_pinned_runs_and_publishes_decision() -> None:
    repository, executor, service = _comparison_context()
    calls_before = executor.calls

    result = asyncio.run(service.run_once())

    assert result.status is WorkerResultStatus.SUCCEEDED
    assert executor.calls == calls_before
    assert repository.completed_decision is not None
    assert repository.completed_decision.decision_id == "decision-worker-001"
    assert repository.completed_decision.decision.status.value == "passed"


def test_blocked_comparison_keeps_event_loop_available_for_heartbeat(
    monkeypatch: MonkeyPatch,
) -> None:
    repository, executor, _unused = _comparison_context()
    sleeper = ManualSleeper()
    service = _service(repository, executor, sleeper=sleeper)
    started = Event()
    release = Event()

    def blocking_compare_runs(
        *,
        spec: EvaluationSpec,
        dataset: DatasetVersion,
        baseline: RunResult,
        candidate: RunResult,
    ) -> ReleaseDecision:
        started.set()
        if not release.wait(timeout=10):
            raise AssertionError("comparison blocked the worker event loop")
        return compare_runs_sync(
            spec=spec,
            dataset=dataset,
            baseline=baseline,
            candidate=candidate,
        )

    monkeypatch.setattr(
        "llm_eval_control_plane.application.worker.compare_runs",
        blocking_compare_runs,
    )

    async def exercise() -> WorkerResultStatus:
        task = asyncio.create_task(service.run_once())
        try:
            assert await asyncio.to_thread(started.wait, 10)
            assert not task.done()
            sleeper.tick.set()
            await asyncio.wait_for(repository.heartbeat_seen.wait(), timeout=10)
            assert not task.done()
        finally:
            release.set()
        result = await asyncio.wait_for(task, timeout=10)
        return result.status

    status = asyncio.run(exercise())

    assert status is WorkerResultStatus.SUCCEEDED
    assert sleeper.calls == 1
    assert repository.heartbeat_calls == 2


def test_explicit_transient_failure_retries_with_bounded_exponential_backoff() -> None:
    executor = FailingExecutor(transient=True)
    repository, _, service = _run_context(executor=executor, attempt_count=2)
    service = _service(
        repository,
        executor,
        backoff_base_seconds=3,
        backoff_max_seconds=20,
    )

    result = asyncio.run(service.run_once())

    assert result.status is WorkerResultStatus.RETRY_SCHEDULED
    assert repository.retry_call == (6, "transient_execution_failure")
    assert repository.fail_code is None


def test_transient_exhaustion_and_permanent_exceptions_fail_safely() -> None:
    transient = FailingExecutor(transient=True)
    exhausted, _, exhausted_service = _run_context(
        executor=transient,
        attempt_count=3,
        max_attempts=3,
    )
    permanent = FailingExecutor(transient=False)
    failed, _, failed_service = _run_context(executor=permanent)

    exhausted_result = asyncio.run(exhausted_service.run_once())
    failed_result = asyncio.run(failed_service.run_once())

    assert exhausted_result.status is WorkerResultStatus.FAILED
    assert exhausted.fail_code == "retry_exhausted"
    assert exhausted.retry_call is None
    assert failed_result.status is WorkerResultStatus.FAILED
    assert failed.fail_code == "execution_failed"
    serialized = f"{failed_result!r} {failed_result}"
    assert "private-payload-sentinel" not in serialized
    assert "private-token" not in serialized
    assert "private-worker-identity" not in serialized


def test_atomic_cancellation_wins_over_retry_and_permanent_failure() -> None:
    transient_executor = FailingExecutor(transient=True)
    transient, _, transient_service = _run_context(executor=transient_executor)
    transient.cancel_during_mutation = True
    permanent_executor = FailingExecutor(transient=False)
    permanent, _, permanent_service = _run_context(executor=permanent_executor)
    permanent.cancel_during_mutation = True

    transient_result = asyncio.run(transient_service.run_once())
    permanent_result = asyncio.run(permanent_service.run_once())

    assert transient_result.status is WorkerResultStatus.CANCELED
    assert transient.retry_call is None
    assert permanent_result.status is WorkerResultStatus.CANCELED
    assert permanent.fail_code is None
    assert transient.job.status is permanent.job.status is JobStatus.CANCELED


def test_final_heartbeat_acknowledges_cancellation_before_publication() -> None:
    repository, executor, service = _run_context()
    repository.heartbeat_status = JobStatus.CANCEL_REQUESTED

    result = asyncio.run(service.run_once())

    assert result.status is WorkerResultStatus.CANCELED
    assert executor.calls == 1
    assert repository.acknowledged is True
    assert repository.completed_run is None


def test_lost_lease_never_publishes_or_mutates_the_new_owner() -> None:
    repository, _, service = _run_context()
    repository.heartbeat_error = StoreLeaseLostError("private lease token")

    result = asyncio.run(service.run_once())

    assert result.status is WorkerResultStatus.LEASE_LOST
    assert repository.completed_run is None
    assert repository.retry_call is None
    assert repository.fail_code is None


def test_heartbeat_runs_during_cooperative_long_execution_without_sleeping() -> None:
    async def exercise() -> tuple[WorkerResultStatus, int, int]:
        executor = BlockingExecutor()
        repository, _, _unused = _run_context(executor=executor)
        sleeper = ManualSleeper()
        service = _service(repository, executor, sleeper=sleeper)
        task = asyncio.create_task(service.run_once())
        await executor.started.wait()
        sleeper.tick.set()
        await repository.heartbeat_seen.wait()
        executor.release.set()
        result = await task
        return result.status, sleeper.calls, repository.heartbeat_calls

    status, sleep_calls, heartbeat_calls = asyncio.run(exercise())

    assert status is WorkerResultStatus.SUCCEEDED
    assert sleep_calls == 1
    assert heartbeat_calls == 2


def test_blocked_repository_evidence_load_still_permits_heartbeat() -> None:
    async def exercise() -> tuple[WorkerResultStatus, int, int]:
        repository, executor, _unused = _run_context()
        repository.block_dataset_load = True
        sleeper = ManualSleeper()
        service = _service(repository, executor, sleeper=sleeper)
        task = asyncio.create_task(service.run_once())
        try:
            assert await asyncio.to_thread(repository.dataset_load_started.wait, 10)
            assert not task.done()
            sleeper.tick.set()
            await asyncio.wait_for(repository.heartbeat_seen.wait(), timeout=10)
            assert not task.done()
        finally:
            repository.dataset_load_release.set()
        result = await asyncio.wait_for(task, timeout=10)
        return result.status, executor.calls, repository.heartbeat_calls

    status, execution_calls, heartbeat_calls = asyncio.run(exercise())

    assert status is WorkerResultStatus.SUCCEEDED
    assert execution_calls == 1
    assert heartbeat_calls == 2


def test_persistence_failure_is_sanitized_and_left_for_lease_recovery() -> None:
    repository, _, service = _run_context()
    repository.heartbeat_error = ControlPlaneStoreError(
        "private-payload-sentinel privateLeaseToken private-worker-identity"
    )

    with raises(WorkerUnavailableError) as captured:
        asyncio.run(service.run_once())

    assert str(captured.value) == "Worker persistence is unavailable"
    assert repository.completed_run is None
    assert repository.retry_call is None
    assert repository.fail_code is None


def test_missing_dependency_and_contract_drift_fail_without_execution() -> None:
    repository, executor, service = _run_context()
    repository.dataset = DatasetRecord(dataset=_dataset(), created_at=NOW)
    repository.dataset = repository.dataset.model_copy(
        update={
            "dataset": DatasetVersion.create(
                name="worker/fixture",
                revision=2,
                cases=repository.dataset.dataset.cases,
            )
        }
    )

    missing = asyncio.run(service.run_once())

    assert missing.status is WorkerResultStatus.FAILED
    assert repository.fail_code == "dependency_not_found"
    assert executor.calls == 0


def test_idle_claims_use_fresh_tokens_and_invalid_private_controls_are_safe() -> None:
    repository, executor, _unused = _run_context()
    repository.claim = False
    tokens = iter(("A" * 32, "B" * 32))

    def unavailable_worker_clock() -> datetime:
        raise AssertionError("worker clock must not timestamp a lease claim")

    service = _service(
        repository,
        executor,
        lease_token_factory=lambda: next(tokens),
        clock=unavailable_worker_clock,
    )

    first = asyncio.run(service.run_once())
    second = asyncio.run(service.run_once())

    assert first.status is second.status is WorkerResultStatus.IDLE
    assert repository.claim_tokens == ["A" * 32, "B" * 32]
    with raises(WorkerConfigurationError) as captured:
        asyncio.run(
            _service(
                repository,
                executor,
                lease_token_factory=lambda: "private-invalid-token",
            ).run_once()
        )
    assert "private-invalid-token" not in str(captured.value)

    def broken_token_factory() -> str:
        raise RuntimeError("private-token-factory-sentinel")

    with raises(WorkerConfigurationError) as broken:
        asyncio.run(
            _service(
                repository,
                executor,
                lease_token_factory=broken_token_factory,
            ).run_once()
        )
    assert "private-token-factory-sentinel" not in str(broken.value)


def test_external_task_cancellation_leaves_the_attempt_for_recovery() -> None:
    async def exercise() -> None:
        executor = BlockingExecutor()
        repository, _, _unused = _run_context(executor=executor)
        task = asyncio.create_task(_service(repository, executor).run_once())
        await executor.started.wait()
        task.cancel()
        with raises(asyncio.CancelledError):
            await task
        assert repository.completed_run is None
        assert repository.retry_call is None
        assert repository.fail_code is None
        assert repository.acknowledged is False

    asyncio.run(exercise())


def test_worker_configuration_defaults_and_claim_failures_are_safe() -> None:
    repository, executor, _unused = _run_context()
    for overrides in (
        {"worker_id": ""},
        {"lease_seconds": 4},
        {"lease_seconds": True},
        {"heartbeat_seconds": 16},
        {"backoff_base_seconds": 5, "backoff_max_seconds": 4},
    ):
        with raises(WorkerConfigurationError):
            _service(repository, executor, **overrides)

    default_service = WorkerService(
        repository=cast(ControlPlaneRepository, repository),
        executor=executor,
        worker_id="default-worker",
    )
    assert (
        asyncio.run(default_service.run_once()).status is WorkerResultStatus.SUCCEEDED
    )

    unavailable, unavailable_executor, _unused = _run_context()
    unavailable.claim_error = ControlPlaneStoreError("private claim details")
    with raises(WorkerUnavailableError) as captured:
        asyncio.run(_service(unavailable, unavailable_executor).run_once())
    assert str(captured.value) == "Worker persistence is unavailable"


def test_worker_wraps_only_claimed_processing_in_the_injected_trace_context() -> None:
    events: list[tuple[str, JobKind, str | None]] = []

    @contextmanager
    def trace_job(kind: JobKind, traceparent: str | None) -> Iterator[None]:
        events.append(("enter", kind, traceparent))
        yield
        events.append(("exit", kind, traceparent))

    executor = CountingExecutor()
    dataset = _dataset()
    repository = FakeRepository(
        job=_job(traceparent=TRACEPARENT),
        payload=_run_payload(dataset, executor),
        dataset=dataset,
    )
    service = _service(repository, executor, trace_job=trace_job)

    assert asyncio.run(service.run_once()).status is WorkerResultStatus.SUCCEEDED
    assert events == [
        ("enter", JobKind.RUN, TRACEPARENT),
        ("exit", JobKind.RUN, TRACEPARENT),
    ]
    assert executor.calls == 1

    repository.claim = False
    assert asyncio.run(service.run_once()).status is WorkerResultStatus.IDLE
    assert len(events) == 2


def test_worker_rejects_malformed_claims_and_payload_kinds() -> None:
    repository, _executor, service = _run_context()
    claim = repository.claim_next_job(
        worker_id="private-worker-identity",
        lease_token="privateLeaseToken_1234567890abcdef",
        lease_seconds=30,
    )
    assert claim is not None
    malformed_claims = (
        (claim, "differentLeaseToken_1234567890abcdef"),
        (
            replace(
                claim,
                job=claim.job.model_copy(update={"status": JobStatus.SUCCEEDED}),
            ),
            claim.lease_token,
        ),
        (
            replace(
                claim,
                attempt=claim.attempt.model_copy(
                    update={"status": JobAttemptStatus.SUCCEEDED}
                ),
            ),
            claim.lease_token,
        ),
        (
            replace(
                claim,
                attempt=claim.attempt.model_copy(update={"job_id": "different-job"}),
            ),
            claim.lease_token,
        ),
        (
            replace(
                claim,
                attempt=claim.attempt.model_copy(update={"attempt_number": 2}),
            ),
            claim.lease_token,
        ),
    )
    for malformed, expected_token in malformed_claims:
        with raises(WorkerInvariantError):
            service._validate_claim(malformed, expected_token)

    mismatched_dataset = DatasetVersion.create(
        name="worker/fixture",
        revision=1,
        cases=(
            EvaluationCase(
                case_id="case-001",
                input=CanonicalJson.from_value("different"),
                expected=CanonicalJson.from_value("different"),
            ),
        ),
    )
    repository.dataset = DatasetRecord(dataset=mismatched_dataset, created_at=NOW)
    mismatch = asyncio.run(service.run_once())
    assert mismatch.status is WorkerResultStatus.FAILED
    assert repository.fail_code == "invalid_job_payload"

    class UnsupportedPayload:
        kind = JobKind.RUN

    unsupported, unsupported_executor, _unused = _run_context()
    unsupported.payload = cast(JobPayload, UnsupportedPayload())
    unsupported_result = asyncio.run(
        _service(unsupported, unsupported_executor).run_once()
    )
    assert unsupported_result.status is WorkerResultStatus.FAILED
    assert unsupported.fail_code == "invalid_job_payload"


def test_kind_mismatch_contract_drift_and_comparison_digest_drift_fail_safely() -> None:
    dataset = _dataset()
    executor = CountingExecutor()
    kind_mismatch = FakeRepository(
        job=_job(kind=JobKind.COMPARISON, resource_id="decision-kind-mismatch"),
        payload=_run_payload(dataset, executor),
        dataset=dataset,
    )
    mismatch_result = asyncio.run(_service(kind_mismatch, executor).run_once())
    assert mismatch_result.status is WorkerResultStatus.FAILED
    assert kind_mismatch.fail_code == "invalid_job_payload"

    class DriftExecutor(CountingExecutor):
        drift = False

        def validate(
            self,
            *,
            target_name: str,
            target_revision: int,
            adapter: str,
            evaluator_names: tuple[str, ...],
            scenario_overrides: Mapping[str, str],
        ) -> ExecutionContract:
            return super().validate(
                target_name=target_name,
                target_revision=target_revision,
                adapter=adapter,
                evaluator_names=evaluator_names,
                scenario_overrides=(
                    {**scenario_overrides, "case-001": "uppercase"}
                    if self.drift
                    else scenario_overrides
                ),
            )

    drift_executor = DriftExecutor()
    drift_repository, _, _unused = _run_context(executor=drift_executor)
    drift_executor.drift = True
    drift_executor.calls = 0
    drift_result = asyncio.run(_service(drift_repository, drift_executor).run_once())
    assert drift_result.status is WorkerResultStatus.FAILED
    assert drift_repository.fail_code == "invalid_job_payload"
    assert drift_executor.calls == 0

    comparison, comparison_executor, _unused = _comparison_context()
    payload = comparison.payload
    assert isinstance(payload, ComparisonJobPayload)
    comparison.payload = payload.model_copy(
        update={"baseline_result_digest": sha256_digest("different-result")}
    )
    comparison_result = asyncio.run(
        _service(comparison, comparison_executor).run_once()
    )
    assert comparison_result.status is WorkerResultStatus.FAILED
    assert comparison.fail_code == "invalid_job_payload"


def test_invalid_worker_clock_fails_the_attempt_without_leaking_details() -> None:
    for clock in (
        lambda: datetime(2026, 8, 23, 12),
        lambda: (_ for _ in ()).throw(RuntimeError("private clock details")),
    ):
        repository, executor, _unused = _run_context()
        result = asyncio.run(_service(repository, executor, clock=clock).run_once())
        assert result.status is WorkerResultStatus.FAILED
        assert repository.fail_code == "execution_failed"


def test_monitor_first_cancellation_and_monitor_failure_stop_execution() -> None:
    async def cancel_during_execution() -> tuple[WorkerResultStatus, bool]:
        executor = BlockingExecutor()
        repository, _, _unused = _run_context(executor=executor)
        repository.heartbeat_status = JobStatus.CANCEL_REQUESTED
        sleeper = ManualSleeper()
        task = asyncio.create_task(
            _service(repository, executor, sleeper=sleeper).run_once()
        )
        await executor.started.wait()
        sleeper.tick.set()
        result = await task
        return result.status, repository.acknowledged

    status, acknowledged = asyncio.run(cancel_during_execution())
    assert status is WorkerResultStatus.CANCELED
    assert acknowledged is True

    async def broken_sleeper(_seconds: float) -> None:
        raise RuntimeError("private sleeper failure")

    blocked = BlockingExecutor()
    repository, _, _unused = _run_context(executor=blocked)
    with raises(WorkerUnavailableError):
        asyncio.run(_service(repository, blocked, sleeper=broken_sleeper).run_once())


def test_retry_mutation_fences_cancellation_and_invalid_states() -> None:
    lease_lost_executor = FailingExecutor(transient=True)
    lease_lost, _, _unused = _run_context(executor=lease_lost_executor)
    lease_lost.retry_error = StoreLeaseLostError("private stale retry")
    result = asyncio.run(_service(lease_lost, lease_lost_executor).run_once())
    assert result.status is WorkerResultStatus.LEASE_LOST

    unavailable_executor = FailingExecutor(transient=True)
    unavailable, _, _unused = _run_context(executor=unavailable_executor)
    unavailable.retry_error = ControlPlaneStoreError("private retry storage")
    with raises(WorkerUnavailableError):
        asyncio.run(_service(unavailable, unavailable_executor).run_once())

    canceled_executor = FailingExecutor(transient=True)
    canceled, _, _unused = _run_context(executor=canceled_executor)
    canceled.retry_status = JobStatus.CANCELED
    canceled_result = asyncio.run(_service(canceled, canceled_executor).run_once())
    assert canceled_result.status is WorkerResultStatus.CANCELED

    invalid_executor = FailingExecutor(transient=True)
    invalid, _, _unused = _run_context(executor=invalid_executor)
    invalid.retry_status = JobStatus.RUNNING
    with raises(WorkerInvariantError):
        asyncio.run(_service(invalid, invalid_executor).run_once())


def test_failure_mutation_fences_cancellation_and_invalid_states() -> None:
    lease_lost_executor = FailingExecutor(transient=False)
    lease_lost, _, _unused = _run_context(executor=lease_lost_executor)
    lease_lost.fail_error = StoreLeaseLostError("private stale failure")
    result = asyncio.run(_service(lease_lost, lease_lost_executor).run_once())
    assert result.status is WorkerResultStatus.LEASE_LOST

    unavailable_executor = FailingExecutor(transient=False)
    unavailable, _, _unused = _run_context(executor=unavailable_executor)
    unavailable.fail_error = ControlPlaneStoreError("private failure storage")
    with raises(WorkerUnavailableError):
        asyncio.run(_service(unavailable, unavailable_executor).run_once())

    canceled_executor = FailingExecutor(transient=False)
    canceled, _, _unused = _run_context(executor=canceled_executor)
    canceled.fail_status = JobStatus.CANCELED
    canceled_result = asyncio.run(_service(canceled, canceled_executor).run_once())
    assert canceled_result.status is WorkerResultStatus.CANCELED

    invalid_executor = FailingExecutor(transient=False)
    invalid, _, _unused = _run_context(executor=invalid_executor)
    invalid.fail_status = JobStatus.RUNNING
    with raises(WorkerInvariantError):
        asyncio.run(_service(invalid, invalid_executor).run_once())


def test_publication_fences_conflicts_storage_and_invalid_states() -> None:
    lease_lost, lease_executor, _unused = _run_context()
    lease_lost.completion_error = StoreLeaseLostError("private stale completion")
    assert (
        asyncio.run(_service(lease_lost, lease_executor).run_once()).status
        is WorkerResultStatus.LEASE_LOST
    )

    conflict, conflict_executor, _unused = _run_context()
    conflict.completion_error = StoreConflictError("private evidence conflict")
    conflict_result = asyncio.run(_service(conflict, conflict_executor).run_once())
    assert conflict_result.status is WorkerResultStatus.FAILED
    assert conflict.fail_code == "evidence_conflict"

    unavailable, unavailable_executor, _unused = _run_context()
    unavailable.completion_error = ControlPlaneStoreError("private completion storage")
    with raises(WorkerUnavailableError):
        asyncio.run(_service(unavailable, unavailable_executor).run_once())

    canceled, canceled_executor, _unused = _run_context()
    canceled.completion_status = JobStatus.CANCELED
    assert (
        asyncio.run(_service(canceled, canceled_executor).run_once()).status
        is WorkerResultStatus.CANCELED
    )

    invalid, invalid_executor, _unused = _run_context()
    invalid.completion_status = JobStatus.RUNNING
    with raises(WorkerInvariantError):
        asyncio.run(_service(invalid, invalid_executor).run_once())


def test_cancellation_acknowledgement_is_fenced_and_validated() -> None:
    lease_lost, lease_executor, _unused = _run_context()
    lease_lost.heartbeat_status = JobStatus.CANCEL_REQUESTED
    lease_lost.acknowledge_error = StoreLeaseLostError("private stale cancellation")
    assert (
        asyncio.run(_service(lease_lost, lease_executor).run_once()).status
        is WorkerResultStatus.LEASE_LOST
    )

    unavailable, unavailable_executor, _unused = _run_context()
    unavailable.heartbeat_status = JobStatus.CANCEL_REQUESTED
    unavailable.acknowledge_error = ControlPlaneStoreError("private ack storage")
    with raises(WorkerUnavailableError):
        asyncio.run(_service(unavailable, unavailable_executor).run_once())

    invalid, invalid_executor, _unused = _run_context()
    invalid.heartbeat_status = JobStatus.CANCEL_REQUESTED
    invalid.acknowledge_status = JobStatus.RUNNING
    with raises(WorkerInvariantError):
        asyncio.run(_service(invalid, invalid_executor).run_once())

    terminal, terminal_executor, _unused = _run_context()
    terminal.heartbeat_status = JobStatus.SUCCEEDED
    assert (
        asyncio.run(_service(terminal, terminal_executor).run_once()).status
        is WorkerResultStatus.LEASE_LOST
    )
