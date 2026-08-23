import asyncio
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import count
from typing import Never

from pytest import MonkeyPatch, fixture, raises
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from llm_eval_control_plane.adapters.control_plane_db import (
    CONTROL_PLANE_METADATA,
    SqlAlchemyControlPlaneRepository,
)
from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.application.control_plane import (
    ComparisonSubmission,
    ControlPlaneService,
    ExecutionContract,
    IdempotencyConflictError,
    InvalidCursorError,
    InvalidSubmissionError,
    ResourceConflictError,
    ResourceNotFoundError,
    RunSubmission,
    StoreConflictError,
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
)
from llm_eval_control_plane.domain.control_plane import JobStatus
from llm_eval_control_plane.domain.results import ExecutionMode, RunResult


class ReadyRepository(SqlAlchemyControlPlaneRepository):
    def schema_is_current(self) -> bool:
        return True


class FailingExecutor(DeterministicEvaluationExecutor):
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
        raise RuntimeError("private-sentinel")


class InvalidResultExecutor(DeterministicEvaluationExecutor):
    def __init__(self, mode: str) -> None:
        self._mode = mode

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
        result = await super().execute(
            run_id=run_id,
            dataset=dataset,
            target_name=target_name,
            target_revision=target_revision,
            adapter=adapter,
            evaluator_names=evaluator_names,
            scenario_overrides=scenario_overrides,
        )
        if self._mode == "run_id":
            return result.model_copy(update={"run_id": "unexpected-run"})
        if self._mode == "dataset":
            different_dataset = ArtifactRef(
                kind=ArtifactKind.DATASET,
                name="different",
                revision=1,
                digest=result.dataset.digest,
            )
            return result.model_copy(update={"dataset": different_dataset})
        if self._mode == "target":
            different_target = ArtifactRef(
                kind=ArtifactKind.TARGET,
                name="fake/different",
                revision=9,
                digest=result.target.digest,
            )
            return result.model_copy(update={"target": different_target})
        if self._mode == "evaluators":
            different_evaluator = ArtifactRef(
                kind=ArtifactKind.EVALUATOR,
                name="builtin/different",
                revision=1,
                digest=result.evaluators[0].digest,
            )
            return result.model_copy(update={"evaluators": (different_evaluator,)})
        if self._mode in {"metric_evaluator", "observation_evaluator"}:
            different_evaluator = ArtifactRef(
                kind=ArtifactKind.EVALUATOR,
                name="builtin/different",
                revision=1,
                digest=result.evaluators[0].digest,
            )
            if self._mode == "metric_evaluator":
                summary = result.metrics[0].model_copy(
                    update={"evaluator": different_evaluator}
                )
                return result.model_copy(update={"metrics": (summary,)})
            observation = (
                result.cases[0]
                .observations[0]
                .model_copy(update={"evaluator": different_evaluator})
            )
            case = result.cases[0].model_copy(update={"observations": (observation,)})
            return result.model_copy(update={"cases": (case,)})
        if self._mode == "mode":
            return result.model_copy(update={"execution_mode": ExecutionMode.LIVE})
        return result.model_copy(update={"cases": ()})


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
        return replace(contract, adapter="different-adapter")


@dataclass(frozen=True, slots=True)
class ServiceContext:
    engine: Engine
    repository: ReadyRepository
    service: ControlPlaneService


def _service(
    repository: ReadyRepository,
    *,
    executor: DeterministicEvaluationExecutor | None = None,
    identifier_factory: Callable[[str], str] | None = None,
) -> ControlPlaneService:
    identifiers = count(1)

    def next_identifier(prefix: str) -> str:
        return f"{prefix}_{next(identifiers):04d}"

    selected_factory = (
        next_identifier if identifier_factory is None else identifier_factory
    )
    return ControlPlaneService(
        repository=repository,
        executor=executor or DeterministicEvaluationExecutor(),
        clock=lambda: datetime(2026, 8, 20, 12, tzinfo=UTC),
        identifier_factory=selected_factory,
    )


def _prefixed_factory(namespace: str) -> Callable[[str], str]:
    identifiers = count(1)

    def factory(prefix: str) -> str:
        return f"{prefix}_{namespace}_{next(identifiers):04d}"

    return factory


@fixture
def context() -> Iterator[ServiceContext]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    CONTROL_PLANE_METADATA.create_all(engine)
    repository = ReadyRepository(engine)
    yield ServiceContext(
        engine=engine,
        repository=repository,
        service=_service(repository),
    )
    engine.dispose()


def _dataset(name: str = "fixture") -> DatasetVersion:
    return DatasetVersion.create(
        name=name,
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
        scenario_overrides={},
    )


def _submit(service: ControlPlaneService, submission: RunSubmission) -> object:
    return asyncio.run(service.submit_run(submission))


def test_read_methods_translate_missing_records_and_invalid_cursors(
    context: ServiceContext,
) -> None:
    service = context.service
    service.register_dataset(_dataset())
    assert service.get_dataset("fixture", 1).dataset.name == "fixture"
    assert service.list_datasets(limit=1).items[0].name == "fixture"

    with raises(ResourceNotFoundError):
        service.get_dataset("missing", 1)
    with raises(ResourceNotFoundError):
        service.get_job("missing")
    with raises(ResourceNotFoundError):
        service.get_run("missing")
    with raises(ResourceNotFoundError):
        service.get_release_decision("missing")
    with raises(InvalidCursorError):
        service.list_datasets(limit=1, cursor="invalid")
    with raises(InvalidCursorError):
        service.list_jobs(limit=1, cursor="invalid")
    with raises(InvalidCursorError):
        service.list_runs(limit=1, cursor="invalid")
    with raises(InvalidCursorError):
        service.list_release_decisions(limit=1, cursor="invalid")


def test_run_preflight_rejects_missing_dataset_and_invalid_adapter(
    context: ServiceContext,
) -> None:
    with raises(ResourceNotFoundError):
        _submit(context.service, _run_submission("missing"))

    context.service.register_dataset(_dataset())
    with raises(InvalidSubmissionError):
        _submit(
            context.service,
            _run_submission("bad-adapter", adapter="private-adapter"),
        )
    assert context.service.list_jobs(limit=10).items == ()

    excessive = DatasetVersion.create(
        name="too-many-slices",
        revision=1,
        cases=(
            EvaluationCase(
                case_id="case-001",
                input=CanonicalJson.from_value({"scenario": "echo"}),
                slices=tuple(f"slice-{index}" for index in range(33)),
            ),
        ),
    )
    with raises(InvalidSubmissionError):
        context.service.register_dataset(excessive)


def test_run_failures_are_safe_terminal_jobs_and_never_replayed(
    context: ServiceContext,
) -> None:
    service = _service(context.repository, executor=FailingExecutor())
    service.register_dataset(_dataset())

    first = asyncio.run(service.submit_run(_run_submission("failed-run")))
    replay = asyncio.run(service.submit_run(_run_submission("failed-run")))

    assert first.created is True
    assert first.job.status is JobStatus.FAILED
    assert first.job.error_code == "execution_failed"
    assert replay.created is False
    assert replay.job == first.job
    assert "private-sentinel" not in first.job.model_dump_json()


def test_invalid_executor_evidence_is_rejected_before_persistence(
    context: ServiceContext,
) -> None:
    context.service.register_dataset(_dataset())
    modes = (
        "run_id",
        "dataset",
        "target",
        "evaluators",
        "metric_evaluator",
        "observation_evaluator",
        "mode",
        "cases",
    )
    for index, mode in enumerate(modes, start=1):
        service = _service(
            context.repository,
            executor=InvalidResultExecutor(mode),
            identifier_factory=_prefixed_factory(mode),
        )
        result = asyncio.run(
            service.submit_run(_run_submission(f"invalid-result-{index}"))
        )
        assert result.job.status is JobStatus.FAILED
        assert result.job.error_code == "execution_failed"
        with raises(ResourceNotFoundError):
            service.get_run(result.job.resource_id)


def test_executor_contract_must_match_submission_before_job_claim(
    context: ServiceContext,
) -> None:
    service = _service(context.repository, executor=InvalidContractExecutor())
    service.register_dataset(_dataset())

    with raises(InvalidSubmissionError):
        asyncio.run(service.submit_run(_run_submission("invalid-contract")))

    assert service.list_jobs(limit=10).items == ()


def test_run_completion_conflict_fails_job_atomically(
    context: ServiceContext,
    monkeypatch: MonkeyPatch,
) -> None:
    context.service.register_dataset(_dataset())

    def conflict(*_args: object, **_kwargs: object) -> Never:
        raise StoreConflictError("private-sentinel")

    monkeypatch.setattr(context.repository, "complete_run", conflict)
    result = asyncio.run(
        context.service.submit_run(_run_submission("evidence-conflict"))
    )

    assert result.job.status is JobStatus.FAILED
    assert result.job.error_code == "evidence_conflict"


def test_job_identity_and_idempotency_conflicts_are_distinct(
    context: ServiceContext,
) -> None:
    context.service.register_dataset(_dataset())
    first = _submit(context.service, _run_submission("same-key"))
    assert first is not None
    with raises(IdempotencyConflictError):
        _submit(
            context.service,
            _run_submission("same-key", target_revision=3),
        )

    collision_service = _service(
        context.repository,
        identifier_factory=lambda _prefix: "collision",
    )
    _submit(collision_service, _run_submission("collision-one"))
    with raises(ResourceConflictError):
        _submit(collision_service, _run_submission("collision-two"))


def test_readiness_sanitizes_connectivity_failures(
    context: ServiceContext,
    monkeypatch: MonkeyPatch,
) -> None:
    assert context.service.ready() is True

    def unavailable() -> Never:
        raise RuntimeError("private-sentinel")

    monkeypatch.setattr(context.repository, "check_health", unavailable)
    assert context.service.ready() is False


def _comparison_spec(
    dataset: DatasetVersion,
    *,
    baseline: RunResult,
    candidate: RunResult,
    metric: str = "quality.exact_match",
    candidate_ref: ArtifactRef | None = None,
) -> EvaluationSpec:
    return EvaluationSpec(
        name="release-policy",
        dataset=dataset.artifact_ref,
        baseline=baseline.target,
        candidate=candidate_ref or candidate.target,
        gates=(
            MetricGate(
                metric=metric,
                direction=MetricDirection.HIGHER_IS_BETTER,
                threshold=1.0,
            ),
        ),
    )


def _comparison_evidence(
    service: ControlPlaneService,
) -> tuple[DatasetVersion, RunResult, RunResult]:
    dataset = _dataset()
    service.register_dataset(dataset)
    baseline_job = asyncio.run(
        service.submit_run(
            _run_submission(
                "baseline",
                target_name="fake/baseline",
                target_revision=1,
            )
        )
    ).job
    candidate_job = asyncio.run(
        service.submit_run(
            _run_submission(
                "candidate",
                target_name="fake/candidate",
                target_revision=2,
            )
        )
    ).job
    return (
        dataset,
        service.get_run(baseline_job.resource_id).result,
        service.get_run(candidate_job.resource_id).result,
    )


def _comparison_submission(
    key: str,
    *,
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


def test_comparison_preflight_and_execution_failures_are_typed(
    context: ServiceContext,
) -> None:
    dataset, baseline, candidate = _comparison_evidence(context.service)
    wrong_candidate = ArtifactRef(
        kind=ArtifactKind.TARGET,
        name="fake/other",
        revision=9,
        digest=candidate.target.digest,
    )
    with raises(InvalidSubmissionError):
        asyncio.run(
            context.service.submit_comparison(
                _comparison_submission(
                    "bad-preflight",
                    dataset=dataset,
                    baseline=baseline,
                    candidate=candidate,
                    spec=_comparison_spec(
                        dataset,
                        baseline=baseline,
                        candidate=candidate,
                        candidate_ref=wrong_candidate,
                    ),
                )
            )
        )

    too_many_gates = EvaluationSpec(
        name="large-policy",
        dataset=dataset.artifact_ref,
        baseline=baseline.target,
        candidate=candidate.target,
        gates=tuple(
            MetricGate(
                metric=f"quality.metric_{index}",
                direction=MetricDirection.HIGHER_IS_BETTER,
                threshold=1.0,
            )
            for index in range(65)
        ),
    )
    with raises(InvalidSubmissionError):
        asyncio.run(
            context.service.submit_comparison(
                _comparison_submission(
                    "too-many-gates",
                    dataset=dataset,
                    baseline=baseline,
                    candidate=candidate,
                    spec=too_many_gates,
                )
            )
        )

    failed = asyncio.run(
        context.service.submit_comparison(
            _comparison_submission(
                "bad-gate",
                dataset=dataset,
                baseline=baseline,
                candidate=candidate,
                spec=_comparison_spec(
                    dataset,
                    baseline=baseline,
                    candidate=candidate,
                    metric="quality.absent",
                ),
            )
        )
    )
    assert failed.job.status is JobStatus.FAILED
    assert failed.job.error_code == "comparison_failed"


def test_comparison_idempotency_and_completion_conflicts(
    context: ServiceContext,
    monkeypatch: MonkeyPatch,
) -> None:
    dataset, baseline, candidate = _comparison_evidence(context.service)
    spec = _comparison_spec(
        dataset,
        baseline=baseline,
        candidate=candidate,
    )
    submission = _comparison_submission(
        "comparison",
        dataset=dataset,
        baseline=baseline,
        candidate=candidate,
        spec=spec,
    )
    first = asyncio.run(context.service.submit_comparison(submission))
    replay = asyncio.run(context.service.submit_comparison(submission))
    assert first.created is True
    assert replay.created is False

    changed_spec = spec.model_copy(update={"name": "changed-policy"})
    with raises(IdempotencyConflictError):
        asyncio.run(
            context.service.submit_comparison(
                _comparison_submission(
                    "comparison",
                    dataset=dataset,
                    baseline=baseline,
                    candidate=candidate,
                    spec=changed_spec,
                )
            )
        )

    def conflict(*_args: object, **_kwargs: object) -> Never:
        raise StoreConflictError("private-sentinel")

    monkeypatch.setattr(
        context.repository,
        "complete_release_decision",
        conflict,
    )
    failed = asyncio.run(
        context.service.submit_comparison(
            _comparison_submission(
                "comparison-conflict",
                dataset=dataset,
                baseline=baseline,
                candidate=candidate,
                spec=spec,
            )
        )
    )
    assert failed.job.status is JobStatus.FAILED
    assert failed.job.error_code == "evidence_conflict"
