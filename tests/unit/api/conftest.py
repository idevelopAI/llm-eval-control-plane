from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count
from typing import cast

from fastapi.testclient import TestClient
from pytest import fixture

from llm_eval_control_plane.api.app import create_app
from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.application.control_plane import (
    ControlPlaneRepository,
    ControlPlaneService,
    StoreConflictError,
    StoreIdempotencyConflictError,
    StoreInvalidCursorError,
    StoreNotFoundError,
    StoreTransitionError,
)
from llm_eval_control_plane.domain.comparison import ReleaseStatus
from llm_eval_control_plane.domain.control_plane import (
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
    RunListRecord,
    RunRecord,
)
from llm_eval_control_plane.domain.datasets import DatasetVersion
from llm_eval_control_plane.domain.results import RunResult

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


class ReadyRepository:
    """HTTP boundary test double independent from lease-adapter implementation."""

    def __init__(self) -> None:
        self.datasets: dict[tuple[str, int], DatasetRecord] = {}
        self.jobs: dict[str, JobRecord] = {}
        self.payloads: dict[str, JobPayload] = {}
        self.runs: dict[str, RunRecord] = {}
        self.decisions: dict[str, ReleaseDecisionRecord] = {}
        self.attempts: dict[str, tuple[JobAttemptRecord, ...]] = {}
        self.current_schema = True

    def put_dataset(self, record: DatasetRecord) -> DatasetRecord:
        key = (record.dataset.name, record.dataset.revision)
        existing = self.datasets.get(key)
        if existing is not None and existing.dataset != record.dataset:
            raise StoreConflictError("private dataset conflict")
        self.datasets[key] = existing or record
        return self.datasets[key]

    def get_dataset(self, name: str, revision: int) -> DatasetRecord:
        try:
            return self.datasets[(name, revision)]
        except KeyError:
            raise StoreNotFoundError("private dataset missing") from None

    def list_datasets(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        name: str | None = None,
    ) -> CursorPage[DatasetListRecord]:
        self._validate_cursor(cursor)
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
        for existing in self.jobs.values():
            if (
                existing.kind is record.kind
                and existing.idempotency_key == record.idempotency_key
            ):
                if existing.request_digest != record.request_digest:
                    raise StoreIdempotencyConflictError("private digest conflict")
                return existing, False
            if (
                existing.kind is record.kind
                and existing.resource_id == record.resource_id
            ):
                raise StoreConflictError("private resource conflict")
        if record.job_id in self.jobs:
            raise StoreConflictError("private job conflict")
        self.jobs[record.job_id] = record
        self.payloads[record.job_id] = payload
        return record, True

    def get_job(self, job_id: str) -> JobRecord:
        try:
            return self.jobs[job_id]
        except KeyError:
            raise StoreNotFoundError("private job missing") from None

    def list_jobs(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        kind: JobKind | None = None,
        status: JobStatus | None = None,
    ) -> CursorPage[JobRecord]:
        self._validate_cursor(cursor)
        items = tuple(
            record
            for record in self.jobs.values()
            if (kind is None or record.kind is kind)
            and (status is None or record.status is status)
        )
        return CursorPage(items=items[:limit])

    def transition_job(
        self,
        job_id: str,
        status: JobStatus,
        *,
        at: datetime = NOW,
        error_code: str | None = None,
    ) -> JobRecord:
        changed = self.get_job(job_id).transition_to(
            status,
            at=at,
            error_code=error_code,
        )
        self.jobs[job_id] = changed
        return changed

    def cancel_job(self, job_id: str) -> JobRecord:
        current = self.get_job(job_id)
        if current.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
            raise StoreTransitionError("private terminal state")
        changed = current.request_cancellation(at=NOW)
        self.jobs[job_id] = changed
        return changed

    def list_job_attempts(self, job_id: str) -> tuple[JobAttemptRecord, ...]:
        return self.attempts.get(job_id, ())

    def get_run(self, run_id: str) -> RunRecord:
        try:
            return self.runs[run_id]
        except KeyError:
            raise StoreNotFoundError("private run missing") from None

    def list_runs(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        dataset_name: str | None = None,
    ) -> CursorPage[RunListRecord]:
        self._validate_cursor(cursor)
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
            raise StoreNotFoundError("private decision missing") from None

    def list_release_decisions(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        status: ReleaseStatus | None = None,
    ) -> CursorPage[ReleaseDecisionListRecord]:
        self._validate_cursor(cursor)
        items = tuple(
            ReleaseDecisionListRecord(
                decision_id=record.decision_id,
                status=record.decision.status,
                baseline_run_id=record.decision.baseline_run_id,
                candidate_run_id=record.decision.candidate_run_id,
                decision_digest=record.decision.decision_digest,
                created_at=record.created_at,
            )
            for record in self.decisions.values()
            if status is None or record.decision.status is status
        )
        return CursorPage(items=items[:limit])

    def check_health(self) -> None:
        return None

    def schema_is_current(self) -> bool:
        return self.current_schema

    def finish_run(self, job_id: str, result: RunResult) -> JobRecord:
        running = self.transition_job(job_id, JobStatus.RUNNING)
        self.runs[result.run_id] = RunRecord(result=result, created_at=NOW)
        succeeded = running.transition_to(JobStatus.SUCCEEDED, at=NOW)
        self.jobs[job_id] = succeeded
        return succeeded

    @staticmethod
    def _validate_cursor(cursor: str | None) -> None:
        if cursor is not None:
            raise StoreInvalidCursorError("private cursor")


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


@dataclass(frozen=True, slots=True)
class ApiHarness:
    client: TestClient
    service: ControlPlaneService
    repository: ReadyRepository
    executor: CountingExecutor


def _identifier_factory() -> Iterator[str]:
    sequence = count(1)
    while True:
        yield f"id_{next(sequence):04d}"


@fixture
def api_harness() -> Iterator[ApiHarness]:
    repository = ReadyRepository()
    executor = CountingExecutor()
    identifiers = _identifier_factory()
    service = ControlPlaneService(
        repository=cast(ControlPlaneRepository, repository),
        executor=executor,
        clock=lambda: NOW,
        identifier_factory=lambda _prefix: next(identifiers),
    )
    with TestClient(
        create_app(service=service),
        raise_server_exceptions=False,
    ) as client:
        yield ApiHarness(
            client=client,
            service=service,
            repository=repository,
            executor=executor,
        )


@fixture
def dataset_body() -> dict[str, object]:
    return {
        "name": "release-gate/offline",
        "revision": 1,
        "cases": [
            {
                "case_id": "echo-001",
                "input": {"scenario": "echo", "value": "private-sentinel"},
                "expected": "private-sentinel",
                "slices": ["core"],
            }
        ],
    }


@fixture
def run_body() -> dict[str, object]:
    return {
        "dataset_name": "release-gate/offline",
        "dataset_revision": 1,
        "target_name": "fake/candidate",
        "target_revision": 2,
        "evaluators": ["exact_match"],
    }
