from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Lock
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pytest import MonkeyPatch
from sqlalchemy import create_engine, delete, or_, select
from sqlalchemy.engine import Engine, make_url

from llm_eval_control_plane.adapters.control_plane_db import (
    SqlAlchemyControlPlaneRepository,
    datasets_table,
    jobs_table,
    release_decisions_table,
    runs_table,
)
from llm_eval_control_plane.api import runtime
from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.domain import sha256_digest
from llm_eval_control_plane.domain.control_plane import JobKind, JobRecord, JobStatus
from llm_eval_control_plane.domain.datasets import DatasetVersion
from llm_eval_control_plane.domain.results import RunResult

_DATASET_NAME = "phase4-integration/restart"
_BASELINE_KEY = "phase4-baseline-001"
_CANDIDATE_KEY = "phase4-candidate-001"
_COMPARISON_KEY = "phase4-comparison-001"
_RACE_KEY = "phase4-race-001"
_TEST_IDEMPOTENCY_KEYS = (
    _BASELINE_KEY,
    _CANDIDATE_KEY,
    _COMPARISON_KEY,
    _RACE_KEY,
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


def _clear_test_records(engine: Engine) -> None:
    test_run_ids = select(runs_table.c.run_id).where(
        runs_table.c.dataset_name == _DATASET_NAME
    )
    with engine.begin() as connection:
        connection.execute(
            delete(release_decisions_table).where(
                or_(
                    release_decisions_table.c.baseline_run_id.in_(test_run_ids),
                    release_decisions_table.c.candidate_run_id.in_(test_run_ids),
                )
            )
        )
        connection.execute(
            delete(runs_table).where(runs_table.c.dataset_name == _DATASET_NAME)
        )
        connection.execute(
            delete(jobs_table).where(
                jobs_table.c.idempotency_key.in_(_TEST_IDEMPOTENCY_KEYS)
            )
        )
        connection.execute(
            delete(datasets_table).where(datasets_table.c.name == _DATASET_NAME)
        )


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    raw_url = os.environ.get("CONTROL_PLANE_DATABASE_URL")
    if raw_url is None:
        pytest.skip("CONTROL_PLANE_DATABASE_URL is required for PostgreSQL integration")
    if make_url(raw_url).get_backend_name() != "postgresql":
        pytest.skip("real PostgreSQL is required for this integration test")

    engine = create_engine(raw_url, pool_pre_ping=True, hide_parameters=True)
    repository = SqlAlchemyControlPlaneRepository(engine)
    repository.check_health()
    assert repository.schema_is_current()
    _clear_test_records(engine)
    try:
        yield engine
    finally:
        _clear_test_records(engine)
        engine.dispose()


def _dataset_body() -> dict[str, object]:
    return {
        "name": _DATASET_NAME,
        "revision": 1,
        "cases": [
            {
                "case_id": "postgres-echo-001",
                "input": {
                    "scenario": "echo",
                    "value": "private-postgres-sentinel",
                },
                "expected": "private-postgres-sentinel",
                "slices": ["integration"],
            }
        ],
    }


def _run_body(*, target_name: str, target_revision: int) -> dict[str, object]:
    return {
        "dataset_name": _DATASET_NAME,
        "dataset_revision": 1,
        "target_name": target_name,
        "target_revision": target_revision,
        "evaluators": ["exact_match"],
    }


def _json_document(response: Response) -> dict[str, Any]:
    document = response.json()
    assert isinstance(document, dict)
    return cast(dict[str, Any], document)


def test_completed_run_replays_after_restart_without_reinvocation(
    postgres_engine: Engine,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CONTROL_PLANE_DATABASE_URL",
        postgres_engine.url.render_as_string(hide_password=False),
    )
    executors: list[CountingExecutor] = []

    def create_executor() -> CountingExecutor:
        executor = CountingExecutor()
        executors.append(executor)
        return executor

    monkeypatch.setattr(runtime, "DeterministicEvaluationExecutor", create_executor)
    baseline_body = _run_body(
        target_name="fake/postgres-baseline",
        target_revision=1,
    )
    candidate_body = _run_body(
        target_name="fake/postgres-candidate",
        target_revision=2,
    )

    with TestClient(
        runtime.create_runtime_app(),
        raise_server_exceptions=False,
    ) as first_client:
        dataset_response = first_client.post("/v1/datasets", json=_dataset_body())
        baseline_response = first_client.post(
            "/v1/runs",
            json=baseline_body,
            headers={"Idempotency-Key": _BASELINE_KEY},
        )
        candidate_response = first_client.post(
            "/v1/runs",
            json=candidate_body,
            headers={"Idempotency-Key": _CANDIDATE_KEY},
        )

        assert dataset_response.status_code == 201
        assert baseline_response.status_code == 201
        assert candidate_response.status_code == 201
        baseline_document = _json_document(baseline_response)
        candidate_document = _json_document(candidate_response)

    assert len(executors) == 1
    assert executors[0].calls == 2

    with TestClient(
        runtime.create_runtime_app(),
        raise_server_exceptions=False,
    ) as replay_client:
        replay_response = replay_client.post(
            "/v1/runs",
            json=candidate_body,
            headers={"Idempotency-Key": _CANDIDATE_KEY},
        )

        assert replay_response.status_code == 200
        assert _json_document(replay_response) == candidate_document
        assert len(executors) == 2
        assert executors[1].calls == 0

        baseline_run = baseline_document["run"]
        candidate_run = candidate_document["run"]
        assert isinstance(baseline_run, dict)
        assert isinstance(candidate_run, dict)
        comparison_response = replay_client.post(
            "/v1/comparisons",
            json={
                "dataset_name": _DATASET_NAME,
                "dataset_revision": 1,
                "baseline_run_id": baseline_run["run_id"],
                "candidate_run_id": candidate_run["run_id"],
                "spec": {
                    "name": "phase4-postgres-release-policy",
                    "dataset": candidate_run["dataset"],
                    "baseline": baseline_run["target"],
                    "candidate": candidate_run["target"],
                    "gates": [
                        {
                            "metric": "quality.exact_match",
                            "direction": "higher_is_better",
                            "threshold": 1.0,
                        }
                    ],
                },
            },
            headers={"Idempotency-Key": _COMPARISON_KEY},
        )

        assert comparison_response.status_code == 201
        comparison_document = _json_document(comparison_response)
        assert comparison_document["job"]["status"] == "succeeded"
        assert comparison_document["decision"]["status"] == "passed"

    assert len(executors) == 2
    assert executors[1].calls == 0

    with TestClient(
        runtime.create_runtime_app(),
        raise_server_exceptions=False,
    ) as durable_client:
        candidate_run = candidate_document["run"]
        candidate_job = candidate_document["job"]
        decision = comparison_document["decision"]
        assert isinstance(candidate_run, dict)
        assert isinstance(candidate_job, dict)
        assert isinstance(decision, dict)

        loaded_run = durable_client.get(f"/v1/runs/{candidate_run['run_id']}")
        loaded_job = durable_client.get(f"/v1/jobs/{candidate_job['job_id']}")
        loaded_dataset = durable_client.get(f"/v1/dataset-revisions/1/{_DATASET_NAME}")
        loaded_decision = durable_client.get(
            f"/v1/release-decisions/{decision['decision_id']}"
        )

        assert loaded_run.status_code == 200
        assert loaded_job.status_code == 200
        assert loaded_dataset.status_code == 200
        assert loaded_decision.status_code == 200
        assert _json_document(loaded_run) == candidate_run
        assert _json_document(loaded_decision) == decision

        safe_documents = json.dumps(
            [
                _json_document(loaded_run),
                _json_document(loaded_job),
                _json_document(loaded_dataset),
                _json_document(loaded_decision),
            ],
            sort_keys=True,
        )
        for private_value in (
            "private-postgres-sentinel",
            _BASELINE_KEY,
            _CANDIDATE_KEY,
            _COMPARISON_KEY,
            "idempotency_key",
            "request_digest",
            '"cases"',
            '"input"',
            '"output"',
            '"expected"',
        ):
            assert private_value not in safe_documents

    assert len(executors) == 3
    assert executors[2].calls == 0


def test_two_postgres_sessions_choose_one_execution_claimant(
    postgres_engine: Engine,
) -> None:
    assert SqlAlchemyControlPlaneRepository(postgres_engine).schema_is_current()
    engine_a = create_engine(
        postgres_engine.url,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    engine_b = create_engine(
        postgres_engine.url,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    repository_a = SqlAlchemyControlPlaneRepository(engine_a)
    repository_b = SqlAlchemyControlPlaneRepository(engine_b)
    request_digest = sha256_digest({"submission": "phase4-concurrent-race"})
    created_at = datetime(2026, 8, 20, 12, tzinfo=UTC)
    barrier = Barrier(2)
    claim_lock = Lock()
    execution_claims = 0

    def claim(
        repository: SqlAlchemyControlPlaneRepository,
        suffix: str,
    ) -> tuple[JobRecord, bool]:
        nonlocal execution_claims
        proposed = JobRecord(
            job_id=f"phase4-race-job-{suffix}",
            kind=JobKind.RUN,
            status=JobStatus.QUEUED,
            idempotency_key=_RACE_KEY,
            request_digest=request_digest,
            resource_id=f"phase4-race-run-{suffix}",
            created_at=created_at,
            updated_at=created_at,
        )
        barrier.wait(timeout=10)
        stored, created = repository.begin_job(proposed)
        if created:
            with claim_lock:
                execution_claims += 1
        return stored, created

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(claim, repository_a, "a")
            second_future = pool.submit(claim, repository_b, "b")
            outcomes = (
                first_future.result(timeout=10),
                second_future.result(timeout=10),
            )
    finally:
        engine_a.dispose()
        engine_b.dispose()

    assert sum(created for _, created in outcomes) == 1
    assert execution_claims == 1
    assert outcomes[0][0].job_id == outcomes[1][0].job_id
    assert outcomes[0][0].resource_id == outcomes[1][0].resource_id
