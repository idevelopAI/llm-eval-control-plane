import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from itertools import count
from typing import cast

from pytest import MonkeyPatch, raises

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
    validate_comparison_inputs,
    validate_execution_contract,
    validate_run_result,
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
from llm_eval_control_plane.domain.comparison import (
    CaseChange,
    GateCaseComparison,
    ReleaseStatus,
)
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
    ListOrder,
    ReleaseDecisionListRecord,
    ReleaseDecisionRecord,
    RunJobPayload,
    RunListRecord,
    RunRecord,
)
from llm_eval_control_plane.domain.execution import (
    ExecutionFailure,
    FailureCode,
    FailureStage,
)
from llm_eval_control_plane.domain.results import ExecutionMode, RunResult

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)
TRACEPARENT_A = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TRACEPARENT_B = "00-7a3ce929d0e0e47364bf92f3577b34da-0ba902b700f067aa-00"


class CountingExecutor(DeterministicEvaluationExecutor):
    """Count execution calls while retaining real deterministic validation."""

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


class RejectingValidationExecutor(DeterministicEvaluationExecutor):
    """Represent a currently unavailable executor that an exact replay must bypass."""

    def __init__(self) -> None:
        self.validate_calls = 0

    def validate(
        self,
        *,
        target_name: str,
        target_revision: int,
        adapter: str,
        evaluator_names: tuple[str, ...],
        scenario_overrides: Mapping[str, str],
    ) -> ExecutionContract:
        del (
            target_name,
            target_revision,
            adapter,
            evaluator_names,
            scenario_overrides,
        )
        self.validate_calls += 1
        raise ValueError("private current executor validation failure")


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
        self.schema_current = True

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

    def get_job_by_idempotency(
        self,
        kind: JobKind,
        idempotency_key: str,
    ) -> JobRecord:
        for record in self.jobs.values():
            if record.kind is kind and record.idempotency_key == idempotency_key:
                return record
        raise StoreNotFoundError("private idempotency details")

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

    def cancel_job(self, job_id: str) -> JobRecord:
        if self.cancel_error is not None:
            raise self.cancel_error
        changed = self.get_job(job_id).request_cancellation(at=NOW)
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
        order: ListOrder = ListOrder.ASCENDING,
    ) -> CursorPage[ReleaseDecisionListRecord]:
        del limit, status, order
        if cursor is not None:
            raise StoreInvalidCursorError("private cursor details")
        return CursorPage(items=())

    def list_release_decision_cases(
        self,
        decision_id: str,
        *,
        limit: int,
        cursor: str | None = None,
        metric: str | None = None,
        gate_slice: str | None = None,
        case_slice: str | None = None,
        change: CaseChange | None = None,
    ) -> CursorPage[GateCaseComparison]:
        del decision_id, limit, metric, gate_slice, case_slice, change
        if cursor is not None:
            raise StoreInvalidCursorError("private cursor details")
        return CursorPage(items=())

    def check_health(self) -> None:
        if not self.healthy:
            raise RuntimeError("private persistence details")

    def schema_is_current(self) -> bool:
        return self.schema_current


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
    traceparent: str | None = None,
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
        traceparent=traceparent,
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
    submission = _run_submission("same-key", traceparent=TRACEPARENT_A)
    replay_submission = _run_submission("same-key", traceparent=TRACEPARENT_B)

    first = asyncio.run(service.submit_run(submission))
    replay = asyncio.run(service.submit_run(replay_submission))

    assert first.created is True
    assert replay.created is False
    assert replay.job == first.job
    assert first.job.traceparent == TRACEPARENT_A
    assert submission.digest_record() == replay_submission.digest_record()
    assert "traceparent" not in submission.digest_record()
    assert TRACEPARENT_A not in repr(submission)
    assert len(repository.jobs) == 1
    assert len(repository.payloads) == 1
    assert executor.calls == 0


def test_submissions_reject_non_w3c_trace_context_without_retaining_content() -> None:
    private_value = f"{TRACEPARENT_A}-private-baggage"
    with raises(ValueError) as run_error:
        _run_submission("invalid-trace", traceparent=private_value)
    assert "private-baggage" not in str(run_error.value)

    repository = MemoryRepository()
    dataset, baseline, candidate = _seed_runs(repository)
    with raises(ValueError) as comparison_error:
        _comparison_submission(
            "invalid-comparison-trace",
            dataset,
            baseline,
            candidate,
            _spec(dataset, baseline, candidate),
            private_value,
        )
    assert "private-baggage" not in str(comparison_error.value)


def test_exact_run_replay_bypasses_missing_dataset_and_current_executor() -> None:
    repository = MemoryRepository()
    initial_service = _service(repository)
    initial_service.register_dataset(_dataset())
    submission = _run_submission("durable-run-replay")
    first = asyncio.run(initial_service.submit_run(submission))
    repository.datasets.clear()
    rejecting_executor = RejectingValidationExecutor()
    replay_service = _service(repository, executor=rejecting_executor)

    replay = asyncio.run(replay_service.submit_run(submission))

    assert replay.job == first.job
    assert replay.created is False
    assert rejecting_executor.validate_calls == 0
    with raises(IdempotencyConflictError):
        asyncio.run(
            replay_service.submit_run(
                _run_submission("durable-run-replay", target_revision=3)
            )
        )
    assert rejecting_executor.validate_calls == 0


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
    traceparent: str | None = None,
) -> ComparisonSubmission:
    return ComparisonSubmission(
        idempotency_key=key,
        dataset_name=dataset.name,
        dataset_revision=dataset.revision,
        baseline_run_id=baseline.run_id,
        candidate_run_id=candidate.run_id,
        spec=spec,
        traceparent=traceparent,
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
        TRACEPARENT_A,
    )
    replay_submission = _comparison_submission(
        "comparison",
        dataset,
        baseline,
        candidate,
        submission.spec,
        TRACEPARENT_B,
    )

    first = asyncio.run(service.submit_comparison(submission))
    replay = asyncio.run(service.submit_comparison(replay_submission))

    assert first.created is True
    assert first.job.status is JobStatus.QUEUED
    assert replay.created is False
    assert replay.job.traceparent == TRACEPARENT_A
    assert submission.digest_record() == replay_submission.digest_record()
    assert "traceparent" not in submission.digest_record()
    assert executor.calls == 0
    payload = repository.payloads[first.job.job_id]
    assert isinstance(payload, ComparisonJobPayload)
    assert payload.dataset == dataset.artifact_ref
    assert payload.baseline_result_digest == baseline.result_digest
    assert payload.candidate_result_digest == candidate.result_digest
    assert payload.spec == submission.spec


def test_exact_comparison_replay_bypasses_removed_dependencies() -> None:
    repository = MemoryRepository()
    dataset, baseline, candidate = _seed_runs(repository)
    service = _service(repository)
    spec = _spec(dataset, baseline, candidate)
    submission = _comparison_submission(
        "durable-comparison-replay",
        dataset,
        baseline,
        candidate,
        spec,
    )
    first = asyncio.run(service.submit_comparison(submission))
    repository.datasets.clear()
    repository.runs.clear()

    replay = asyncio.run(service.submit_comparison(submission))

    assert replay.job == first.job
    assert replay.created is False
    changed = submission.spec.model_copy(update={"name": "changed-policy"})
    with raises(IdempotencyConflictError):
        asyncio.run(
            service.submit_comparison(
                _comparison_submission(
                    "durable-comparison-replay",
                    dataset,
                    baseline,
                    candidate,
                    changed,
                )
            )
        )


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

    def unavailable_api_clock() -> datetime:
        raise AssertionError("API clock must not timestamp cancellation")

    service._clock = unavailable_api_clock
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


def test_service_configuration_reads_and_schema_readiness_are_bounded() -> None:
    repository = MemoryRepository()
    service = _service(repository)

    for invalid in (True, 0, 11):
        with raises(ValueError, match="maximum attempts"):
            ControlPlaneService(
                repository=cast(ControlPlaneRepository, repository),
                executor=DeterministicEvaluationExecutor(),
                max_attempts=invalid,
            )

    with raises(ResourceNotFoundError):
        service.get_dataset("missing", 1)
    with raises(ResourceNotFoundError):
        service.get_job("missing")
    with raises(ResourceNotFoundError):
        service.get_run("missing")
    with raises(ResourceNotFoundError):
        service.get_release_decision("missing")

    repository.schema_current = False
    assert service.ready() is False


def test_dataset_bounds_are_translated_without_content_leaks(
    monkeypatch: MonkeyPatch,
) -> None:
    import llm_eval_control_plane.application.control_plane as control_plane_module

    repository = MemoryRepository()
    service = _service(repository)
    dataset = _dataset()

    monkeypatch.setattr(control_plane_module, "_MAX_DATASET_CASES", 0)
    with raises(InvalidSubmissionError, match="service limits"):
        service.register_dataset(dataset)

    monkeypatch.setattr(control_plane_module, "_MAX_DATASET_CASES", 1)
    monkeypatch.setattr(control_plane_module, "_MAX_SLICES_PER_CASE", 0)
    sliced = DatasetVersion.create(
        name="sliced",
        revision=1,
        cases=(
            EvaluationCase(
                case_id="case-001",
                input=CanonicalJson.from_value("input"),
                slices=("private-slice",),
            ),
        ),
    )
    with raises(InvalidSubmissionError) as per_case:
        service.register_dataset(sliced)
    assert "private-slice" not in str(per_case.value)

    monkeypatch.setattr(control_plane_module, "_MAX_SLICES_PER_CASE", 1)
    monkeypatch.setattr(control_plane_module, "_MAX_DATASET_SLICES", 0)
    with raises(InvalidSubmissionError):
        service.register_dataset(sliced)


def test_execution_contract_and_run_evidence_must_match_pinned_inputs() -> None:
    dataset = _dataset()
    dataset_record = DatasetRecord(dataset=dataset, created_at=NOW)
    executor = DeterministicEvaluationExecutor()
    contract = executor.validate(
        target_name="fake/candidate",
        target_revision=2,
        adapter="deterministic_fake",
        evaluator_names=("exact_match",),
        scenario_overrides={},
    )
    result = asyncio.run(
        _result(
            run_id="run-candidate",
            dataset=dataset,
            target_name="fake/candidate",
            target_revision=2,
        )
    )

    for arguments in (
        {"adapter": "different"},
        {"evaluator_names": ("different",)},
        {"target_name": "fake/different"},
    ):
        values: dict[str, object] = {
            "target_name": "fake/candidate",
            "target_revision": 2,
            "adapter": "deterministic_fake",
            "evaluator_names": ("exact_match",),
        }
        values.update(arguments)
        with raises(ValueError):
            validate_execution_contract(contract, **values)  # type: ignore[arg-type]

    other_evaluator = ArtifactRef(
        kind=ArtifactKind.EVALUATOR,
        name="builtin/other",
        revision=1,
        digest=sha256_digest("other evaluator"),
    )
    other_dataset = ArtifactRef(
        kind=ArtifactKind.DATASET,
        name="different",
        revision=1,
        digest=sha256_digest("other dataset"),
    )
    other_target = ArtifactRef(
        kind=ArtifactKind.TARGET,
        name="fake/different",
        revision=1,
        digest=sha256_digest("other target"),
    )
    metric = result.metrics[0].model_copy(update={"evaluator": other_evaluator})
    observation = (
        result.cases[0]
        .observations[0]
        .model_copy(update={"evaluator": other_evaluator})
    )
    observation_case = result.cases[0].model_copy(
        update={"observations": (observation,)}
    )
    failure = ExecutionFailure(
        stage=FailureStage.EVALUATOR,
        code=FailureCode.EVALUATOR_EXCEPTION,
        message="safe failure",
        evaluator=other_evaluator,
    )
    failure_case = result.cases[0].model_copy(update={"evaluator_failures": (failure,)})
    variants = (
        result.model_copy(update={"run_id": "different-run"}),
        result.model_copy(update={"dataset": other_dataset}),
        result.model_copy(update={"target": other_target}),
        result.model_copy(update={"evaluators": (other_evaluator,)}),
        result.model_copy(update={"metrics": (metric,)}),
        result.model_copy(update={"cases": (observation_case,)}),
        result.model_copy(update={"cases": (failure_case,)}),
        result.model_copy(update={"execution_mode": ExecutionMode.LIVE}),
        result.model_copy(
            update={
                "cases": (
                    result.cases[0].model_copy(update={"case_id": "different-case"}),
                )
            }
        ),
    )
    for variant in variants:
        with raises(ValueError):
            validate_run_result(
                variant,
                resource_id=result.run_id,
                dataset=dataset_record,
                contract=contract,
            )


def test_comparison_input_identities_and_work_limits_are_enforced(
    monkeypatch: MonkeyPatch,
) -> None:
    import llm_eval_control_plane.application.control_plane as control_plane_module

    repository = MemoryRepository()
    dataset, baseline, candidate = _seed_runs(repository)
    dataset_record = repository.get_dataset(dataset.name, dataset.revision)
    baseline_record = repository.get_run(baseline.run_id)
    candidate_record = repository.get_run(candidate.run_id)
    spec = _spec(dataset, baseline, candidate)
    valid: dict[str, object] = {
        "dataset_name": dataset.name,
        "dataset_revision": dataset.revision,
        "baseline_run_id": baseline.run_id,
        "candidate_run_id": candidate.run_id,
        "spec": spec,
        "dataset": dataset_record,
        "baseline": baseline_record,
        "candidate": candidate_record,
    }
    mismatches = (
        {"dataset_name": "different"},
        {
            "spec": spec.model_copy(
                update={
                    "dataset": ArtifactRef(
                        kind=ArtifactKind.DATASET,
                        name="different",
                        revision=1,
                        digest=sha256_digest("different dataset"),
                    )
                }
            )
        },
        {"baseline_run_id": "different-baseline"},
        {"candidate_run_id": "different-candidate"},
        {"spec": spec.model_copy(update={"baseline": candidate.target})},
        {"spec": spec.model_copy(update={"candidate": baseline.target})},
    )
    for mismatch in mismatches:
        arguments = {**valid, **mismatch}
        with raises(ValueError):
            validate_comparison_inputs(**arguments)  # type: ignore[arg-type]

    limits = (
        ("_MAX_COMPARISON_GATES", 0),
        ("_MAX_COMPARISON_METRICS", 0),
        ("_MAX_COMPARISON_AGGREGATE_WORK", 0),
        ("_MAX_COMPARISON_CASE_RECORDS", 0),
    )
    for name, value in limits:
        with monkeypatch.context() as context:
            context.setattr(control_plane_module, name, value)
            with raises(ValueError):
                validate_comparison_inputs(**valid)  # type: ignore[arg-type]
