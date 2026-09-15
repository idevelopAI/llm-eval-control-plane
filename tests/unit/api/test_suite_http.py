from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from llm_eval_control_plane.api.app import create_app
from llm_eval_control_plane.api.contracts import SuiteCreateRequest
from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.api.security import ControlPlaneScope
from llm_eval_control_plane.application.comparison import compare_runs
from llm_eval_control_plane.domain.control_plane import (
    ComparisonJobPayload,
    JobStatus,
    ReleaseDecisionRecord,
    RunJobPayload,
)
from llm_eval_control_plane.observability import Observability

from .conftest import AUTH_HEADERS, NOW, ApiHarness, build_authorizer


def _suite_body(harness: ApiHarness, dataset_body: dict[str, object]) -> dict[str, Any]:
    assert harness.client.post("/v1/datasets", json=dataset_body).status_code == 201
    dataset = harness.repository.get_dataset("release-gate/offline", 1).dataset
    contract = DeterministicEvaluationExecutor().validate_suite(
        adapter="deterministic_fake", evaluator_names=("exact_match",)
    )
    return {
        "name": "release/core",
        "revision": 1,
        "dataset": dataset.artifact_ref.model_dump(mode="json"),
        "evaluators": [item.model_dump(mode="json") for item in contract.evaluators],
        "execution": contract.execution.model_dump(mode="json"),
        "slices": ["core"],
        "gates": [
            {
                "metric": "quality.exact_match",
                "direction": "higher_is_better",
                "threshold": 1.0,
                "slice": "core",
            }
        ],
    }


def test_suite_registration_detail_and_bounded_metadata_are_redacted(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
) -> None:
    body = _suite_body(api_harness, dataset_body)
    response = api_harness.client.post("/v1/suites", json=body)
    assert response.status_code == 201
    document = response.json()
    assert set(document) == {
        "schema_version",
        "name",
        "revision",
        "digest",
        "dataset",
        "evaluators",
        "execution",
        "slices",
        "gates",
        "created_at",
    }
    assert document["schema_version"] == "suite-summary/v1"
    assert (
        document["digest"] == SuiteCreateRequest.model_validate(body).to_domain().digest
    )
    assert document["execution"]["execution_mode"] == "offline_mock"
    assert "private-sentinel" not in response.text
    loaded = api_harness.client.get("/v1/suite-revisions/1/release/core")
    assert loaded.status_code == 200
    assert loaded.json() == document
    page = api_harness.client.get("/v1/suites?name=release/core&limit=1").json()
    assert page["schema_version"] == "suite-page/v1"
    assert page["next_cursor"] is None
    assert page["items"] == [
        {
            "schema_version": "suite-list-item/v1",
            "name": "release/core",
            "revision": 1,
            "digest": document["digest"],
            "dataset_name": "release-gate/offline",
            "dataset_revision": 1,
            "evaluator_count": 1,
            "metric_count": 1,
            "slice_count": 1,
            "gate_count": 1,
            "execution_mode": "offline_mock",
            "created_at": document["created_at"],
        }
    ]
    assert api_harness.client.get("/v1/suites?name=absent").json()["items"] == []
    assert api_harness.client.get("/v1/suites?limit=101").status_code == 422
    bad_cursor = api_harness.client.get("/v1/suites?cursor=private-cursor")
    assert bad_cursor.status_code == 400
    assert "private-cursor" not in bad_cursor.text
    assert (
        api_harness.client.get("/v1/suite-revisions/2/release/core").status_code == 404
    )
    assert api_harness.repository.jobs == {}


def test_suite_registration_is_create_once_and_replays_without_revalidation(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
) -> None:
    body = _suite_body(api_harness, dataset_body)
    created = api_harness.client.post("/v1/suites", json=body)
    changed = deepcopy(body)
    changed["gates"][0]["threshold"] = 0.5
    assert api_harness.client.post("/v1/suites", json=changed).status_code == 409
    api_harness.repository.datasets.clear()
    replay = api_harness.client.post("/v1/suites", json=body)
    assert replay.status_code == 201
    assert replay.json() == created.json()
    assert (
        api_harness.client.post("/v1/suites", json={**body, "revision": 2}).status_code
        == 404
    )


@pytest.mark.parametrize(
    "change",
    [
        "unresolved_dataset",
        "wrong_dataset_kind",
        "dataset_drift",
        "evaluator_drift",
        "metrics_drift",
        "duplicate_evaluator",
        "unknown_slice",
        "unknown_gate",
        "live_mode",
        "extra_secret",
        "boolean_revision",
        "boolean_concurrency",
        "boolean_threshold",
        "string_threshold",
        "too_many_gates",
    ],
)
def test_suite_definition_rejects_drift_and_invalid_inputs_without_writes(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
    change: str,
) -> None:
    body = _suite_body(api_harness, dataset_body)
    if change == "unresolved_dataset":
        body["dataset"].pop("digest")
    elif change == "wrong_dataset_kind":
        body["dataset"]["kind"] = "target"
    elif change == "dataset_drift":
        body["dataset"]["digest"] = "sha256:" + "0" * 64
    elif change == "evaluator_drift":
        body["evaluators"][0]["artifact"]["revision"] = 999
    elif change == "metrics_drift":
        body["evaluators"][0]["metrics"].append("private-metric")
    elif change == "duplicate_evaluator":
        body["evaluators"] *= 2
    elif change == "unknown_slice":
        body["slices"].append("private-slice")
    elif change == "unknown_gate":
        body["gates"][0]["metric"] = "private-metric"
    elif change == "live_mode":
        body["execution"]["execution_mode"] = "live"
    elif change == "extra_secret":
        body["execution"]["private-secret"] = "private-sentinel"
    elif change == "boolean_revision":
        body["revision"] = True
    elif change == "boolean_concurrency":
        body["execution"]["max_concurrency"] = True
    elif change == "boolean_threshold":
        body["gates"][0]["threshold"] = True
    elif change == "string_threshold":
        body["gates"][0]["threshold"] = "1.0"
    else:
        body["gates"] *= 65
    response = api_harness.client.post("/v1/suites", json=body)
    assert response.status_code == 422
    assert "private-" not in response.text
    assert api_harness.repository.suites == {}
    assert api_harness.repository.jobs == {}


def test_suite_http_lifecycle_pins_evidence_and_replays_terminal_jobs(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
) -> None:
    body = _suite_body(api_harness, dataset_body)
    assert api_harness.client.post("/v1/suites", json=body).status_code == 201
    suite = api_harness.repository.get_suite("release/core", 1).suite
    run_ids: list[str] = []
    for role in ("baseline", "candidate"):
        submission = {
            "suite_name": suite.name,
            "suite_revision": suite.revision,
            "target_name": f"fake/{role}",
            "target_revision": 1,
            "scenario_overrides": {"echo-001": "uppercase"}
            if role == "candidate"
            else {},
        }
        headers = {"Idempotency-Key": f"suite-{role}"}
        queued = api_harness.client.post(
            "/v1/suite-runs", json=submission, headers=headers
        )
        assert queued.status_code == 202
        job = queued.json()["job"]
        assert queued.headers["location"] == f"/v1/jobs/{job['job_id']}"
        assert queued.json()["run"] is None
        replay = api_harness.client.post(
            "/v1/suite-runs", json=submission, headers=headers
        )
        assert replay.status_code == 202
        assert replay.json() == queued.json()
        payload = api_harness.repository.payloads[job["job_id"]]
        assert isinstance(payload, RunJobPayload)
        assert payload.schema_version == "run-job/v2"
        assert payload.suite == suite
        result = asyncio.run(
            DeterministicEvaluationExecutor().execute_suite(
                run_id=job["resource_id"],
                dataset=api_harness.repository.get_dataset(
                    "release-gate/offline", 1
                ).dataset,
                target_name=payload.target_name,
                target_revision=payload.target_revision,
                scenario_overrides={
                    item.case_id: item.scenario for item in payload.scenario_overrides
                },
                suite=suite,
            )
        )
        api_harness.repository.finish_run(job["job_id"], result)
        finished = api_harness.client.post(
            "/v1/suite-runs", json=submission, headers=headers
        )
        assert finished.status_code == 200
        assert finished.json()["run"]["suite"] == suite.artifact_ref.model_dump(
            mode="json"
        )
        assert "private-sentinel" not in finished.text
        loaded = api_harness.client.get(f"/v1/runs/{result.run_id}")
        assert loaded.json() == finished.json()["run"]
        run_ids.append(result.run_id)

    comparison = {
        "suite_name": suite.name,
        "suite_revision": suite.revision,
        "baseline_run_id": run_ids[0],
        "candidate_run_id": run_ids[1],
    }
    headers = {"Idempotency-Key": "suite-compare"}
    queued = api_harness.client.post(
        "/v1/suite-comparisons", json=comparison, headers=headers
    )
    assert queued.status_code == 202
    job = queued.json()["job"]
    assert queued.headers["location"] == f"/v1/jobs/{job['job_id']}"
    payload = api_harness.repository.payloads[job["job_id"]]
    assert isinstance(payload, ComparisonJobPayload)
    assert payload.suite == suite
    decision = compare_runs(
        spec=payload.spec,
        suite=suite,
        dataset=api_harness.repository.get_dataset("release-gate/offline", 1).dataset,
        baseline=api_harness.repository.get_run(run_ids[0]).result,
        candidate=api_harness.repository.get_run(run_ids[1]).result,
    )
    api_harness.repository.transition_job(job["job_id"], JobStatus.RUNNING)
    api_harness.repository.decisions[job["resource_id"]] = ReleaseDecisionRecord(
        decision_id=job["resource_id"],
        decision=decision,
        created_at=NOW,
    )
    api_harness.repository.transition_job(job["job_id"], JobStatus.SUCCEEDED)
    history_params = {"suite_name": suite.name, "suite_revision": "1", "limit": "1"}
    run_history = api_harness.client.get("/v1/suite-runs", params=history_params)
    assert run_history.status_code == 200
    assert run_history.json()["schema_version"] == "suite-run-history-page/v1"
    run_item = run_history.json()["items"][0]
    assert run_item["schema_version"] == "suite-run-history-item/v1"
    assert run_item["suite"] == suite.artifact_ref.model_dump(mode="json")
    assert run_item["target"] == api_harness.repository.get_run(
        run_item["run_id"]
    ).result.target.model_dump(mode="json")
    assert set(run_item) == {
        "schema_version",
        "run_id",
        "status",
        "execution_mode",
        "dataset_name",
        "dataset_revision",
        "result_digest",
        "created_at",
        "suite",
        "target",
    }
    decision_history = api_harness.client.get(
        "/v1/suite-comparisons", params=history_params
    )
    assert decision_history.status_code == 200
    assert decision_history.json()["schema_version"] == "suite-decision-history-page/v1"
    decision_item = decision_history.json()["items"][0]
    assert decision_item["schema_version"] == "suite-decision-history-item/v1"
    assert decision_item["decision_digest"] == decision.decision_digest
    assert decision_item["baseline_run_id"] == run_ids[0]
    assert decision_item["candidate_run_id"] == run_ids[1]
    assert set(decision_item) == {
        "schema_version",
        "decision_id",
        "status",
        "baseline_run_id",
        "candidate_run_id",
        "decision_digest",
        "created_at",
        "suite",
    }
    assert "private-sentinel" not in run_history.text + decision_history.text
    api_harness.repository.suites.clear()
    for history_path in ("/v1/suite-runs", "/v1/suite-comparisons"):
        assert (
            api_harness.client.get(history_path, params=history_params).status_code
            == 404
        )
    replay = api_harness.client.post(
        "/v1/suite-comparisons", json=comparison, headers=headers
    )
    assert replay.status_code == 200
    assert replay.json()["decision"]["status"] == "failed"
    assert replay.json()["decision"]["suite"] == suite.artifact_ref.model_dump(
        mode="json"
    )
    assert "private-sentinel" not in replay.text
    assert (
        api_harness.client.get(f"/v1/release-decisions/{job['resource_id']}").json()
        == replay.json()["decision"]
    )
    conflict = api_harness.client.post(
        "/v1/suite-comparisons",
        json={**comparison, "suite_revision": 2},
        headers=headers,
    )
    assert conflict.status_code == 409


@pytest.mark.parametrize("path", ["/v1/suite-runs", "/v1/suite-comparisons"])
def test_suite_history_requires_read_scope_and_correct_project_before_validation(
    api_harness: ApiHarness, path: str
) -> None:
    for granted, headers, expected in (
        (ControlPlaneScope.READ, {}, 401),
        (ControlPlaneScope.WRITE, AUTH_HEADERS, 403),
        (
            ControlPlaneScope.READ,
            {**AUTH_HEADERS, "X-Project-ID": "wrong-project"},
            403,
        ),
        (ControlPlaneScope.READ, AUTH_HEADERS, 422),
    ):
        with TestClient(
            create_app(
                service=api_harness.service,
                authorizer=build_authorizer((granted,)),
                telemetry=Observability(service="api"),
            ),
            headers=headers,
        ) as client:
            response = client.get(path, params={"suite_revision": "private-sentinel"})
        assert response.status_code == expected
        assert "private-sentinel" not in response.text
    assert api_harness.repository.jobs == {}


@pytest.mark.parametrize("path", ["/v1/suite-runs", "/v1/suite-comparisons"])
def test_suite_history_rejects_bad_cursors_and_bounds_without_echoing_input(
    api_harness: ApiHarness, dataset_body: dict[str, object], path: str
) -> None:
    body = _suite_body(api_harness, dataset_body)
    assert api_harness.client.post("/v1/suites", json=body).status_code == 201
    params = {"suite_name": "release/core", "suite_revision": "1"}
    response = api_harness.client.get(path, params=params)
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["next_cursor"] is None
    for change, expected in (
        ({"suite_revision": "2"}, 404),
        ({"suite_name": "missing"}, 404),
        ({"cursor": "private-sentinel"}, 400),
        ({"suite_name": "private-sentinel!"}, 422),
        ({"limit": "101"}, 422),
        ({"limit": "0"}, 422),
        ({"suite_revision": "0"}, 422),
    ):
        invalid = api_harness.client.get(path, params={**params, **change})
        assert invalid.status_code == expected
        assert "private-sentinel" not in invalid.text
    assert api_harness.repository.jobs == {}


@pytest.mark.parametrize(
    "path", ["/v1/suites", "/v1/suite-runs", "/v1/suite-comparisons"]
)
def test_suite_mutations_authenticate_before_parsing_and_enforce_write_scope(
    api_harness: ApiHarness,
    path: str,
) -> None:
    for granted, headers, expected in (
        (ControlPlaneScope.WRITE, {}, 401),
        (ControlPlaneScope.READ, AUTH_HEADERS, 403),
        (
            ControlPlaneScope.WRITE,
            {**AUTH_HEADERS, "X-Project-ID": "wrong-project"},
            403,
        ),
    ):
        with TestClient(
            create_app(
                service=api_harness.service,
                authorizer=build_authorizer((granted,)),
                telemetry=Observability(service="api"),
            ),
            headers=headers,
        ) as client:
            response = client.post(
                path,
                content='{ "private-sentinel":',
                headers={"Content-Type": "application/json"},
            )
        assert response.status_code == expected
        assert "private-sentinel" not in response.text
    assert api_harness.repository.suites == {}
    assert api_harness.repository.jobs == {}


def test_suite_submissions_reject_replacement_policy_and_require_idempotency(
    api_harness: ApiHarness,
) -> None:
    for path, body in (
        ("/v1/suite-runs", {"suite_name": "release/core", "suite_revision": 1}),
        (
            "/v1/suite-comparisons",
            {
                "suite_name": "release/core",
                "suite_revision": 1,
                "baseline_run_id": "one",
                "candidate_run_id": "two",
            },
        ),
    ):
        assert api_harness.client.post(path, json=body).status_code == 422
        missing = api_harness.client.post(
            path, json=body, headers={"Idempotency-Key": "missing"}
        )
        assert missing.status_code == 404
        for field in ("spec", "gates", "dataset_name", "evaluators", "execution"):
            invalid = api_harness.client.post(
                path,
                json={**body, field: "private-sentinel"},
                headers={"Idempotency-Key": "invalid"},
            )
            assert invalid.status_code == 422
            assert "private-sentinel" not in invalid.text
    assert api_harness.repository.jobs == {}


def test_suite_telemetry_uses_templates_not_protocol_content(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
) -> None:
    body = _suite_body(api_harness, dataset_body)
    body["name"] = "private-protocol/suite"
    lines: list[str] = []
    telemetry = Observability(service="api", log_sink=lines.append)
    with TestClient(
        create_app(
            service=api_harness.service,
            authorizer=build_authorizer(),
            telemetry=telemetry,
        ),
        headers=AUTH_HEADERS,
    ) as client:
        assert client.post("/v1/suites", json=body).status_code == 201
        assert (
            client.get("/v1/suite-revisions/1/private-protocol/suite").status_code
            == 200
        )
        for path in ("/v1/suite-runs", "/v1/suite-comparisons"):
            assert (
                client.get(
                    path,
                    params={
                        "suite_name": "private-protocol/suite",
                        "suite_revision": 1,
                    },
                ).status_code
                == 200
            )
    requests = [
        event
        for line in lines
        if (event := json.loads(line)).get("event") == "http.request.completed"
    ]
    assert [event["http_route"] for event in requests] == [
        "/v1/suites",
        "/v1/suite-revisions/{revision}/{name:path}",
        "/v1/suite-runs",
        "/v1/suite-comparisons",
    ]
    assert "private-protocol" not in "".join(lines)
    assert "private-protocol" not in telemetry.render_metrics().body.decode()
