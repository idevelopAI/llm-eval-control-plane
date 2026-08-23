from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count

from fastapi.testclient import TestClient
from pytest import fixture
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from llm_eval_control_plane.adapters.control_plane_db import (
    CONTROL_PLANE_METADATA,
    SqlAlchemyControlPlaneRepository,
)
from llm_eval_control_plane.api.app import create_app
from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.application.control_plane import ControlPlaneService
from llm_eval_control_plane.domain.datasets import DatasetVersion
from llm_eval_control_plane.domain.results import RunResult


class ReadyRepository(SqlAlchemyControlPlaneRepository):
    def schema_is_current(self) -> bool:
        return True


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
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    CONTROL_PLANE_METADATA.create_all(engine)
    repository = ReadyRepository(engine)
    executor = CountingExecutor()
    identifiers = _identifier_factory()
    service = ControlPlaneService(
        repository=repository,
        executor=executor,
        clock=lambda: datetime(2026, 8, 20, 12, tzinfo=UTC),
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
    engine.dispose()


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
