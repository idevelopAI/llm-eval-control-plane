from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast

from pytest import raises

from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.application.control_plane import (
    ClaimedJob,
    ControlPlaneRepository,
    ControlPlaneStoreError,
    StoreLeaseLostError,
    StoreNotFoundError,
)
from llm_eval_control_plane.application.worker import (
    TransientExecutionError,
    WorkerConfigurationError,
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
    RunResult,
    sha256_digest,
)
from llm_eval_control_plane.domain.control_plane import (
    ComparisonJobPayload,
    DatasetRecord,
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
    )


class CountingExecutor(DeterministicEvaluationExecutor):
    def __init__(self) -> None:
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
        self.claim_tokens: list[str] = []
        self.claim_worker_ids: list[str] = []
        self.claim_lease_seconds: list[int] = []
        self.heartbeat_calls = 0
        self.heartbeat_lease_seconds: list[int] = []
        self.heartbeat_status = JobStatus.RUNNING
        self.heartbeat_error: ControlPlaneStoreError | None = None
        self.heartbeat_seen = asyncio.Event()
        self.completed_run: RunRecord | None = None
        self.completed_decision: ReleaseDecisionRecord | None = None
        self.retry_call: tuple[int, str] | None = None
        self.fail_code: str | None = None
        self.acknowledged = False
        self.cancel_during_mutation = False

    def claim_next_job(
        self,
        *,
        worker_id: str,
        lease_token: str,
        lease_seconds: int,
    ) -> ClaimedJob | None:
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
        return self.job

    def get_dataset(self, name: str, revision: int) -> DatasetRecord:
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
        if self.cancel_during_mutation:
            return self._cancel()
        self.retry_call = (delay_seconds, error_code)
        self.job = self.job.transition_to(
            JobStatus.QUEUED,
            at=NOW,
            available_at=NOW + timedelta(seconds=delay_seconds),
        )
        return self.job

    def fail_job(
        self,
        _job_id: str,
        _attempt_number: int,
        _lease_token: str,
        *,
        error_code: str,
    ) -> JobRecord:
        if self.cancel_during_mutation:
            return self._cancel()
        self.fail_code = error_code
        self.job = self.job.transition_to(
            JobStatus.FAILED,
            at=NOW,
            error_code=error_code,
        )
        return self.job

    def acknowledge_cancellation(
        self,
        _job_id: str,
        _attempt_number: int,
        _lease_token: str,
    ) -> JobRecord:
        self.acknowledged = True
        self.job = self.job.transition_to(JobStatus.CANCELED, at=NOW)
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
        if self.cancel_during_mutation:
            return self._cancel()
        self.completed_run = record
        self.job = self.job.transition_to(JobStatus.SUCCEEDED, at=NOW)
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
        if self.cancel_during_mutation:
            return self._cancel()
        self.completed_decision = record
        self.job = self.job.transition_to(JobStatus.SUCCEEDED, at=NOW)
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


def test_comparison_attempt_uses_pinned_runs_and_publishes_decision() -> None:
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
    calls_before = executor.calls

    result = asyncio.run(_service(repository, executor).run_once())

    assert result.status is WorkerResultStatus.SUCCEEDED
    assert executor.calls == calls_before
    assert repository.completed_decision is not None
    assert repository.completed_decision.decision_id == "decision-worker-001"
    assert repository.completed_decision.decision.status.value == "passed"


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
