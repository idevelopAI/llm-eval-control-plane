import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from itertools import count
from typing import cast

from pytest import raises

from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.application.control_plane import (
    ComparisonSubmission,
    ControlPlaneRepository,
    ControlPlaneService,
    ExecutionContract,
    IdempotencyConflictError,
    InvalidCursorError,
    InvalidSubmissionError,
    ResourceConflictError,
    ResourceNotFoundError,
    RunSubmission,
    StoreConflictError,
    StoreIdempotencyConflictError,
    StoreInvalidCursorError,
    StoreNotFoundError,
    StoreTransitionError,
)
from llm_eval_control_plane.domain import (
    ArtifactKind,
    ArtifactRef,
    CanonicalJson,
    DatasetVersion,
    EvaluationCase,
    EvaluationSpec,
    MetricDirection,
    MetricGate,
    sha256_digest,
)
from llm_eval_control_plane.domain.comparison import ReleaseStatus
from llm_eval_control_plane.domain.control_plane import (
    ComparisonJobPayload,
    CursorPage,
    DatasetListRecord,
    DatasetRecord,
    JobAttemptRecord,
    JobKind,
    JobPayload,
    JobRecord,
    JobStatus,
    ReleaseDecisionListRecord,
    ReleaseDecisionRecord,
    RunJobPayload,
    RunListRecord,
    RunRecord,
)
from llm_eval_control_plane.domain.results import RunResult

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


class CountingExecutor(DeterministicEvaluationExecutor):
    """Count execution calls while retaining real deterministic validation."""

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


class InvalidContractExecutor(DeterministicEvaluationExecutor):
    def validate(
        self,
        *,
        target_name: str,
        target_revision: int,
        adapter: str,
        evaluator_names: tuple[str, ...],
        scenario_overrides: Mapping[str, str],
    ) -> ExecutionContract:
        contract = super().validate(
            target_name=target_name,
            target_revision=target_revision,
            adapter=adapter,
            evaluator_names=evaluator_names,
            scenario_overrides=scenario_overrides,
        )
        return contract.model_copy(update={"adapter": "different_adapter"})


class MemoryRepository:
    """Application test double for immutable records and atomic job/payload claims."""

    def __init__(self) -> None:
        self.datasets: dict[tuple[str, int], DatasetRecord] = {}
        self.jobs: dict[str, JobRecord] = {}
        self.payloads: dict[str, JobPayload] = {}
        self.runs: dict[str, RunRecord] = {}
        self.decisions: dict[str, ReleaseDecisionRecord] = {}
        self.attempts: dict[str, tuple[JobAttemptRecord, ...]] = {}
        self.begin_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.healthy = True

    def put_dataset(self, record: DatasetRecord) -> DatasetRecord:
        key = (record.dataset.name, record.dataset.revision)
        existing = self.datasets.get(key)
        if existing is not None and existing.dataset != record.dataset:
            raise StoreConflictError("private dataset details")
        self.datasets[key] = existing or record
        return self.datasets[key]

    def get_dataset(self, name: str, revision: int) -> DatasetRecord:
        try:
            return self.datasets[(name, revision)]
        except KeyError:
            raise StoreNotFoundError("private dataset details") from None

    def list_datasets(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        name: str | None = None,
    ) -> CursorPage[DatasetListRecord]:
        if cursor is not None:
            raise StoreInvalidCursorError("private cursor details")
        items = tuple(
            DatasetListRecord(
                name=record.dataset.name,
                revision=record.dataset.revision,
                digest=record.dataset.digest,
                case_count=len(record.dataset.cases),
                created_at=record.created_at,
            )
            for record in self.datasets.values()
            if name is None or record.dataset.name == name
        )
        return CursorPage(items=items[:limit])

    def begin_job(
        self,
        record: JobRecord,
        payload: JobPayload,
    ) -> tuple[JobRecord, bool]:
        if self.begin_error is not None:
            raise self.begin_error
        for existing in self.jobs.values():
            if (
                existing.kind is record.kind
                and existing.idempotency_key == record.idempotency_key
            ):
                if existing.request_digest != record.request_digest:
                    raise StoreIdempotencyConflictError("private digest details")
                return existing, False
            if (
                existing.kind is record.kind
                and existing.resource_id == record.resource_id
            ):
                raise StoreConflictError("private identity details")
        if record.job_id in self.jobs:
            raise StoreConflictError("private identity details")
        self.jobs[record.job_id] = record
        self.payloads[record.job_id] = payload
        return record, True

    def get_job(self, job_id: str) -> JobRecord:
        try:
            return self.jobs[job_id]
        except KeyError:
            raise StoreNotFoundError("private job details") from None

    def list_jobs(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        kind: JobKind | None = None,
        status: JobStatus | None = None,
    ) -> CursorPage[JobRecord]:
        if cursor is not None:
            raise StoreInvalidCursorError("private cursor details")
        items = tuple(
            record
            for record in self.jobs.values()
            if (kind is None or record.kind is kind)
            and (status is None or record.status is status)
        )
        return CursorPage(items=items[:limit])

    def cancel_job(self, job_id: str, *, at: datetime) -> JobRecord:
        if self.cancel_error is not None:
            raise self.cancel_error
        changed = self.get_job(job_id).request_cancellation(at=at)
        self.jobs[job_id] = changed
        return changed

    def list_job_attempts(self, job_id: str) -> tuple[JobAttemptRecord, ...]:
        return self.attempts.get(job_id, ())

    def get_run(self, run_id: str) -> RunRecord:
        try:
            return self.runs[run_id]
        except KeyError:
            raise StoreNotFoundError("private run details") from None

    def list_runs(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        dataset_name: str | None = None,
    ) -> CursorPage[RunListRecord]:
        if cursor is not None:
            raise StoreInvalidCursorError("private cursor details")
        items = tuple(
            RunListRecord(
                run_id=record.result.run_id,
                status=record.result.status,
                execution_mode=record.result.execution_mode,
                dataset_name=record.result.dataset.name,
                dataset_revision=record.result.dataset.revision,
                result_digest=record.result.result_digest,
                created_at=record.created_at,
            )
            for record in self.runs.values()
            if dataset_name is None or record.result.dataset.name == dataset_name
        )
        return CursorPage(items=items[:limit])

    def get_release_decision(self, decision_id: str) -> ReleaseDecisionRecord:
        try:
            return self.decisions[decision_id]
        except KeyError:
            raise StoreNotFoundError("private decision details") from None

    def list_release_decisions(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        status: ReleaseStatus | None = None,
    ) -> CursorPage[ReleaseDecisionListRecord]:
        del limit, status
        if cursor is not None:
            raise StoreInvalidCursorError("private cursor details")
        return CursorPage(items=())

    def check_health(self) -> None:
        if not self.healthy:
            raise RuntimeError("private persistence details")

    def schema_is_current(self) -> bool:
        return True


def _dataset() -> DatasetVersion:
    return DatasetVersion.create(
        name="fixture",
        revision=1,
        cases=(
            EvaluationCase(
                case_id="case-001",
                input=CanonicalJson.from_value({"scenario": "echo", "value": "answer"}),
                expected=CanonicalJson.from_value("answer"),
            ),
        ),
    )


def _run_submission(
    key: str,
    *,
    target_name: str = "fake/candidate",
    target_revision: int = 2,
    adapter: str = "deterministic_fake",
) -> RunSubmission:
    return RunSubmission(
        idempotency_key=key,
        dataset_name="fixture",
        dataset_revision=1,
        target_name=target_name,
        target_revision=target_revision,
        adapter=adapter,
        evaluator_names=("exact_match",),
        scenario_overrides={"case-001": "uppercase"},
    )


def _service(
    repository: MemoryRepository,
    *,
    executor: DeterministicEvaluationExecutor | None = None,
    identifier_factory: Callable[[str], str] | None = None,
) -> ControlPlaneService:
    identifiers = count(1)

    def next_identifier(prefix: str) -> str:
        return f"{prefix}_{next(identifiers):04d}"

    return ControlPlaneService(
        repository=cast(ControlPlaneRepository, repository),
        executor=executor or DeterministicEvaluationExecutor(),
        clock=lambda: NOW,
        identifier_factory=identifier_factory or next_identifier,
    )


def test_run_submission_enqueues_pinned_payload_without_execution() -> None:
    repository = MemoryRepository()
    executor = CountingExecutor()
    service = _service(repository, executor=executor)
    dataset = _dataset()
    service.register_dataset(dataset)
    submission = _run_submission("run-key")

    outcome = asyncio.run(service.submit_run(submission))

    assert outcome.created is True
    assert outcome.job.status is JobStatus.QUEUED
    assert outcome.job.attempt_count == 0
    assert outcome.job.max_attempts == 3
    assert outcome.job.available_at == NOW
    assert outcome.job.request_digest == sha256_digest(submission.digest_record())
    assert executor.calls == 0

    payload = repository.payloads[outcome.job.job_id]
    assert isinstance(payload, RunJobPayload)
    assert payload.dataset == dataset.artifact_ref
    assert payload.execution_contract.target.digest is not None
    assert payload.execution_contract.evaluators[0].digest is not None
    assert payload.scenario_overrides[0].case_id == "case-001"
    assert submission.idempotency_key not in payload.model_dump_json()


def test_run_replay_keeps_one_job_payload_and_zero_execution_calls() -> None:
    repository = MemoryRepository()
    executor = CountingExecutor()
    service = _service(repository, executor=executor)
    service.register_dataset(_dataset())
    submission = _run_submission("same-key")

    first = asyncio.run(service.submit_run(submission))
    replay = asyncio.run(service.submit_run(submission))

    assert first.created is True
    assert replay.created is False
    assert replay.job == first.job
    assert len(repository.jobs) == 1
    assert len(repository.payloads) == 1
    assert executor.calls == 0


def test_run_preflight_and_repository_errors_are_content_safe() -> None:
    repository = MemoryRepository()
    service = _service(repository)
    with raises(ResourceNotFoundError) as missing:
        asyncio.run(service.submit_run(_run_submission("missing")))
    assert "private" not in str(missing.value)

    service.register_dataset(_dataset())
    with raises(InvalidSubmissionError) as invalid:
        asyncio.run(
            service.submit_run(
                _run_submission("invalid", adapter="private_adapter"),
            )
        )
    assert "private_adapter" not in str(invalid.value)
    assert repository.jobs == {}

    repository.begin_error = StoreIdempotencyConflictError("private digest")
    with raises(IdempotencyConflictError) as conflict:
        asyncio.run(service.submit_run(_run_submission("conflict")))
    assert "private" not in str(conflict.value)

    repository.begin_error = StoreConflictError("private identity")
    with raises(ResourceConflictError) as identity:
        asyncio.run(service.submit_run(_run_submission("identity")))
    assert "private" not in str(identity.value)


def test_changed_semantics_conflict_under_the_same_key() -> None:
    repository = MemoryRepository()
    service = _service(repository)
    service.register_dataset(_dataset())
    asyncio.run(service.submit_run(_run_submission("same-key")))

    with raises(IdempotencyConflictError):
        asyncio.run(
            service.submit_run(
                _run_submission("same-key", target_revision=3),
            )
        )


def test_invalid_resolved_contract_is_rejected_before_claim() -> None:
    repository = MemoryRepository()
    service = _service(repository, executor=InvalidContractExecutor())
    service.register_dataset(_dataset())

    with raises(InvalidSubmissionError):
        asyncio.run(service.submit_run(_run_submission("invalid-contract")))
    assert repository.jobs == {}
    assert repository.payloads == {}


async def _result(
    *,
    run_id: str,
    dataset: DatasetVersion,
    target_name: str,
    target_revision: int,
) -> RunResult:
    return await DeterministicEvaluationExecutor().execute(
        run_id=run_id,
        dataset=dataset,
        target_name=target_name,
        target_revision=target_revision,
        adapter="deterministic_fake",
        evaluator_names=("exact_match",),
        scenario_overrides={},
    )


def _seed_runs(
    repository: MemoryRepository,
) -> tuple[DatasetVersion, RunResult, RunResult]:
    dataset = _dataset()
    repository.put_dataset(DatasetRecord(dataset=dataset, created_at=NOW))
    baseline = asyncio.run(
        _result(
            run_id="run-baseline",
            dataset=dataset,
            target_name="fake/baseline",
            target_revision=1,
        )
    )
    candidate = asyncio.run(
        _result(
            run_id="run-candidate",
            dataset=dataset,
            target_name="fake/candidate",
            target_revision=2,
        )
    )
    repository.runs[baseline.run_id] = RunRecord(result=baseline, created_at=NOW)
    repository.runs[candidate.run_id] = RunRecord(result=candidate, created_at=NOW)
    return dataset, baseline, candidate


def _spec(
    dataset: DatasetVersion,
    baseline: RunResult,
    candidate: RunResult,
) -> EvaluationSpec:
    return EvaluationSpec(
        name="release-policy",
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


def _comparison_submission(
    key: str,
    dataset: DatasetVersion,
    baseline: RunResult,
    candidate: RunResult,
    spec: EvaluationSpec,
) -> ComparisonSubmission:
    return ComparisonSubmission(
        idempotency_key=key,
        dataset_name=dataset.name,
        dataset_revision=dataset.revision,
        baseline_run_id=baseline.run_id,
        candidate_run_id=candidate.run_id,
        spec=spec,
    )


def test_comparison_enqueues_immutable_result_digests_and_replays() -> None:
    repository = MemoryRepository()
    dataset, baseline, candidate = _seed_runs(repository)
    executor = CountingExecutor()
    service = _service(repository, executor=executor)
    submission = _comparison_submission(
        "comparison",
        dataset,
        baseline,
        candidate,
        _spec(dataset, baseline, candidate),
    )

    first = asyncio.run(service.submit_comparison(submission))
    replay = asyncio.run(service.submit_comparison(submission))

    assert first.created is True
    assert first.job.status is JobStatus.QUEUED
    assert replay.created is False
    assert executor.calls == 0
    payload = repository.payloads[first.job.job_id]
    assert isinstance(payload, ComparisonJobPayload)
    assert payload.dataset == dataset.artifact_ref
    assert payload.baseline_result_digest == baseline.result_digest
    assert payload.candidate_result_digest == candidate.result_digest
    assert payload.spec == submission.spec


def test_comparison_preflight_and_idempotency_conflicts_are_safe() -> None:
    repository = MemoryRepository()
    dataset, baseline, candidate = _seed_runs(repository)
    service = _service(repository)
    spec = _spec(dataset, baseline, candidate)
    submission = _comparison_submission(
        "comparison",
        dataset,
        baseline,
        candidate,
        spec,
    )
    asyncio.run(service.submit_comparison(submission))

    changed = spec.model_copy(update={"name": "changed-policy"})
    with raises(IdempotencyConflictError):
        asyncio.run(
            service.submit_comparison(
                _comparison_submission(
                    "comparison",
                    dataset,
                    baseline,
                    candidate,
                    changed,
                )
            )
        )

    wrong = ArtifactRef(
        kind=ArtifactKind.TARGET,
        name="fake/other",
        revision=9,
        digest=candidate.target.digest,
    )
    invalid_spec = spec.model_copy(update={"candidate": wrong})
    with raises(InvalidSubmissionError) as invalid:
        asyncio.run(
            service.submit_comparison(
                _comparison_submission(
                    "invalid-comparison",
                    dataset,
                    baseline,
                    candidate,
                    invalid_spec,
                )
            )
        )
    assert "fake/other" not in str(invalid.value)


def test_cancellation_delegates_and_translates_conflicts_safely() -> None:
    repository = MemoryRepository()
    service = _service(repository)
    service.register_dataset(_dataset())
    queued = asyncio.run(service.submit_run(_run_submission("cancel"))).job

    canceled = service.cancel_job(queued.job_id)
    assert canceled.status is JobStatus.CANCELED
    assert service.cancel_job(queued.job_id) == canceled

    repository.cancel_error = StoreTransitionError("private lease state")
    with raises(ResourceConflictError) as conflict:
        service.cancel_job(queued.job_id)
    assert "private" not in str(conflict.value)

    repository.cancel_error = StoreNotFoundError("private row")
    with raises(ResourceNotFoundError) as missing:
        service.cancel_job("missing")
    assert "private" not in str(missing.value)


def test_attempt_listing_and_cursor_errors_remain_repository_owned() -> None:
    repository = MemoryRepository()
    service = _service(repository)
    service.register_dataset(_dataset())
    job = asyncio.run(service.submit_run(_run_submission("attempts"))).job

    assert service.list_job_attempts(job.job_id) == ()
    with raises(ResourceNotFoundError):
        service.list_job_attempts("missing")
    with raises(InvalidCursorError):
        service.list_jobs(limit=1, cursor="private")
    with raises(InvalidCursorError):
        service.list_datasets(limit=1, cursor="private")
    with raises(InvalidCursorError):
        service.list_runs(limit=1, cursor="private")
    with raises(InvalidCursorError):
        service.list_release_decisions(limit=1, cursor="private")


def test_job_identity_collision_and_readiness_failure_are_safe() -> None:
    repository = MemoryRepository()
    service = _service(
        repository,
        identifier_factory=lambda _prefix: "collision",
    )
    service.register_dataset(_dataset())
    asyncio.run(service.submit_run(_run_submission("first")))

    with raises(ResourceConflictError):
        asyncio.run(service.submit_run(_run_submission("second")))

    assert service.ready() is True
    repository.healthy = False
    assert service.ready() is False
