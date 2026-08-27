import asyncio
import json
from datetime import timedelta
from typing import Never, cast

from pytest import MonkeyPatch

from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.application.comparison import compare_runs
from llm_eval_control_plane.application.control_plane import ControlPlaneStoreError
from llm_eval_control_plane.domain import EvaluationSpec, MetricDirection, MetricGate
from llm_eval_control_plane.domain.control_plane import (
    JobAttemptRecord,
    JobAttemptStatus,
    JobStatus,
    ReleaseDecisionRecord,
)

from .conftest import NOW, ApiHarness


def _create_dataset(
    harness: ApiHarness,
    dataset_body: dict[str, object],
) -> None:
    response = harness.client.post("/v1/datasets", json=dataset_body)
    assert response.status_code == 201


def _submit_run(
    harness: ApiHarness,
    body: dict[str, object],
    *,
    key: str,
) -> dict[str, object]:
    response = harness.client.post(
        "/v1/runs",
        json=body,
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202
    document = response.json()
    assert isinstance(document, dict)
    return document


def _object_part(document: dict[str, object], name: str) -> dict[str, object]:
    value = document[name]
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _finish_run(
    harness: ApiHarness,
    document: dict[str, object],
    body: dict[str, object],
) -> None:
    job = document["job"]
    assert isinstance(job, dict)
    run_id = job["resource_id"]
    assert isinstance(run_id, str)
    dataset_name = body["dataset_name"]
    dataset_revision = body["dataset_revision"]
    assert isinstance(dataset_name, str)
    assert isinstance(dataset_revision, int)
    dataset = harness.repository.get_dataset(dataset_name, dataset_revision).dataset
    target_name = body["target_name"]
    target_revision = body["target_revision"]
    evaluators = body["evaluators"]
    assert isinstance(target_name, str)
    assert isinstance(target_revision, int)
    assert isinstance(evaluators, list)
    result = asyncio.run(
        DeterministicEvaluationExecutor().execute(
            run_id=run_id,
            dataset=dataset,
            target_name=target_name,
            target_revision=target_revision,
            adapter="deterministic_fake",
            evaluator_names=tuple(str(item) for item in evaluators),
            scenario_overrides={},
        )
    )
    job_id = job["job_id"]
    assert isinstance(job_id, str)
    harness.repository.finish_run(job_id, result)


def _submit_and_finish_run(
    harness: ApiHarness,
    body: dict[str, object],
    *,
    key: str,
) -> dict[str, object]:
    queued = _submit_run(harness, body, key=key)
    _finish_run(harness, queued, body)
    replay = harness.client.post(
        "/v1/runs",
        json=body,
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 200
    document = replay.json()
    assert isinstance(document, dict)
    return document


def _seed_release_decision(
    harness: ApiHarness,
    dataset_body: dict[str, object],
    run_body: dict[str, object],
) -> str:
    _create_dataset(harness, dataset_body)
    baseline_document = _submit_and_finish_run(
        harness,
        {**run_body, "target_name": "fake/baseline", "target_revision": 1},
        key="dashboard-baseline",
    )
    candidate_document = _submit_and_finish_run(
        harness,
        {**run_body, "target_name": "fake/candidate", "target_revision": 2},
        key="dashboard-candidate",
    )
    baseline_id = _object_part(baseline_document, "run")["run_id"]
    candidate_id = _object_part(candidate_document, "run")["run_id"]
    assert isinstance(baseline_id, str)
    assert isinstance(candidate_id, str)
    baseline = harness.repository.get_run(baseline_id).result
    candidate = harness.repository.get_run(candidate_id).result
    data = harness.repository.get_dataset("release-gate/offline", 1).dataset
    decision = compare_runs(
        spec=EvaluationSpec(
            name="dashboard-policy",
            dataset=data.artifact_ref,
            baseline=baseline.target,
            candidate=candidate.target,
            gates=(
                MetricGate(
                    metric="quality.exact_match",
                    direction=MetricDirection.HIGHER_IS_BETTER,
                    threshold=1.0,
                ),
            ),
        ),
        dataset=data,
        baseline=baseline,
        candidate=candidate,
    )
    decision_id = "decision-dashboard"
    harness.repository.decisions[decision_id] = ReleaseDecisionRecord(
        decision_id=decision_id,
        decision=decision,
        created_at=NOW,
    )
    return decision_id


def test_dataset_registration_and_slash_safe_retrieval_are_redacted(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
) -> None:
    response = api_harness.client.post("/v1/datasets", json=dataset_body)

    assert response.status_code == 201
    assert response.json() == {
        "schema_version": "dataset-summary/v1",
        "name": "release-gate/offline",
        "revision": 1,
        "digest": response.json()["digest"],
        "case_count": 1,
        "created_at": "2026-08-23T12:00:00Z",
    }
    assert "private-sentinel" not in response.text

    loaded = api_harness.client.get("/v1/dataset-revisions/1/release-gate/offline")
    listed = api_harness.client.get("/v1/datasets?name=release-gate/offline&limit=1")
    assert loaded.status_code == 200
    assert loaded.json() == response.json()
    assert listed.json()["items"] == [
        {
            **response.json(),
            "schema_version": "dataset-list-item/v1",
        }
    ]


def test_immutable_dataset_retry_and_conflict(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
) -> None:
    first = api_harness.client.post("/v1/datasets", json=dataset_body)
    retry = api_harness.client.post("/v1/datasets", json=dataset_body)
    changed = json.loads(json.dumps(dataset_body))
    changed["cases"][0]["expected"] = "private-changed-value"
    conflict = api_harness.client.post("/v1/datasets", json=changed)

    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "resource_conflict"
    assert "private-changed-value" not in conflict.text


def test_run_submission_is_enqueue_only_idempotent_and_redacted(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
    run_body: dict[str, object],
) -> None:
    _create_dataset(api_harness, dataset_body)

    first = api_harness.client.post(
        "/v1/runs",
        json=run_body,
        headers={"Idempotency-Key": "run-request-001"},
    )
    replay = api_harness.client.post(
        "/v1/runs",
        json={
            **run_body,
            "adapter": "deterministic_fake",
            "scenario_overrides": {},
        },
        headers={"Idempotency-Key": "run-request-001"},
    )

    assert first.status_code == replay.status_code == 202
    assert replay.json() == first.json()
    assert first.headers["location"] == f"/v1/jobs/{first.json()['job']['job_id']}"
    assert api_harness.executor.calls == 0
    document = first.json()
    assert document["schema_version"] == "run-submission/v2"
    assert document["run"] is None
    assert document["job"] == {
        "schema_version": "job/v2",
        "job_id": document["job"]["job_id"],
        "kind": "run",
        "status": "queued",
        "resource_id": document["job"]["resource_id"],
        "attempt_count": 0,
        "max_attempts": 3,
        "available_at": "2026-08-23T12:00:00Z",
        "error_code": None,
        "created_at": "2026-08-23T12:00:00Z",
        "updated_at": "2026-08-23T12:00:00Z",
    }
    loaded = api_harness.client.get(f"/v1/jobs/{document['job']['job_id']}")
    assert loaded.json() == document["job"]

    serialized = json.dumps(document, sort_keys=True)
    for private_value in (
        "private-sentinel",
        "run-request-001",
        "idempotency_key",
        "request_digest",
        "execution_contract",
        "lease_token",
        "worker_id",
        '"input"',
        '"output"',
        '"expected"',
    ):
        assert private_value not in serialized


def test_running_replay_is_accepted_and_terminal_replay_is_ok(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
    run_body: dict[str, object],
) -> None:
    _create_dataset(api_harness, dataset_body)
    queued = _submit_run(api_harness, run_body, key="worker-run")
    job = queued["job"]
    assert isinstance(job, dict)
    job_id = job["job_id"]
    assert isinstance(job_id, str)
    api_harness.repository.transition_job(job_id, JobStatus.RUNNING)

    running = api_harness.client.post(
        "/v1/runs",
        json=run_body,
        headers={"Idempotency-Key": "worker-run"},
    )
    assert running.status_code == 202
    assert running.json()["job"]["status"] == "running"
    assert running.json()["job"]["attempt_count"] == 1
    assert running.json()["run"] is None

    _finish_run(api_harness, running.json(), run_body)
    terminal = api_harness.client.post(
        "/v1/runs",
        json=run_body,
        headers={"Idempotency-Key": "worker-run"},
    )
    assert terminal.status_code == 200
    assert terminal.json()["job"]["status"] == "succeeded"
    assert terminal.json()["run"]["schema_version"] == "run-summary/v1"
    assert api_harness.executor.calls == 0


def test_queued_cancellation_requires_strict_empty_object_and_is_terminal(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
    run_body: dict[str, object],
) -> None:
    _create_dataset(api_harness, dataset_body)
    queued = _submit_run(api_harness, run_body, key="cancel-queued")
    job_id = _object_part(queued, "job")["job_id"]
    assert isinstance(job_id, str)

    missing_body = api_harness.client.post(f"/v1/jobs/{job_id}/cancellation")
    unknown = api_harness.client.post(
        f"/v1/jobs/{job_id}/cancellation",
        json={"reason": "private-cancellation-reason"},
    )
    canceled = api_harness.client.post(
        f"/v1/jobs/{job_id}/cancellation",
        json={},
    )

    assert missing_body.status_code == 415
    assert unknown.status_code == 422
    assert "private-cancellation-reason" not in unknown.text
    assert canceled.status_code == 200
    assert canceled.json()["schema_version"] == "job/v2"
    assert canceled.json()["status"] == "canceled"
    assert canceled.json()["attempt_count"] == 0

    repeated = api_harness.client.post(
        f"/v1/jobs/{job_id}/cancellation",
        json={},
    )
    assert repeated.status_code == 200
    assert repeated.json() == canceled.json()


def test_running_cancellation_attempts_and_new_status_filter_are_safe(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
    run_body: dict[str, object],
) -> None:
    _create_dataset(api_harness, dataset_body)
    queued = _submit_run(api_harness, run_body, key="cancel-running")
    job_id = _object_part(queued, "job")["job_id"]
    assert isinstance(job_id, str)
    running = api_harness.repository.transition_job(job_id, JobStatus.RUNNING)
    api_harness.repository.attempts[job_id] = (
        JobAttemptRecord(
            job_id=job_id,
            attempt_number=running.attempt_count,
            status=JobAttemptStatus.RUNNING,
            started_at=NOW,
            heartbeat_at=NOW,
            lease_expires_at=NOW + timedelta(seconds=30),
        ),
    )

    attempts = api_harness.client.get(f"/v1/jobs/{job_id}/attempts")
    canceled = api_harness.client.post(
        f"/v1/jobs/{job_id}/cancellation",
        json={},
    )
    filtered = api_harness.client.get(
        "/v1/jobs?kind=run&status=cancel_requested&limit=10"
    )

    assert attempts.status_code == 200
    assert attempts.json() == {
        "schema_version": "job-attempt-list/v1",
        "items": [
            {
                "schema_version": "job-attempt/v1",
                "attempt_number": 1,
                "status": "running",
                "error_code": None,
                "started_at": "2026-08-23T12:00:00Z",
                "heartbeat_at": "2026-08-23T12:00:00Z",
                "lease_expires_at": "2026-08-23T12:00:30Z",
                "finished_at": None,
            }
        ],
    }
    serialized_attempts = json.dumps(attempts.json(), sort_keys=True)
    for private_value in ("lease_token", "worker_id", "job_id", "idempotency_key"):
        assert private_value not in serialized_attempts
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "cancel_requested"
    assert filtered.status_code == 200
    assert filtered.json()["schema_version"] == "job-page/v2"
    assert [item["job_id"] for item in filtered.json()["items"]] == [job_id]


def test_terminal_cancellation_conflicts_without_leaking_state(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
    run_body: dict[str, object],
) -> None:
    _create_dataset(api_harness, dataset_body)
    terminal = _submit_and_finish_run(
        api_harness,
        run_body,
        key="terminal-cancel",
    )
    job_id = _object_part(terminal, "job")["job_id"]
    assert isinstance(job_id, str)

    response = api_harness.client.post(
        f"/v1/jobs/{job_id}/cancellation",
        json={},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "resource_conflict"
    assert "terminal" not in response.text


def test_comparison_submission_enqueues_pinned_runs_and_replays(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
    run_body: dict[str, object],
) -> None:
    _create_dataset(api_harness, dataset_body)
    baseline = _object_part(
        _submit_and_finish_run(
            api_harness,
            {**run_body, "target_name": "fake/baseline", "target_revision": 1},
            key="baseline-run",
        ),
        "run",
    )
    candidate = _object_part(
        _submit_and_finish_run(
            api_harness,
            {**run_body, "target_name": "fake/candidate", "target_revision": 2},
            key="candidate-run",
        ),
        "run",
    )
    body = {
        "dataset_name": "release-gate/offline",
        "dataset_revision": 1,
        "baseline_run_id": baseline["run_id"],
        "candidate_run_id": candidate["run_id"],
        "spec": {
            "name": "release-policy",
            "dataset": candidate["dataset"],
            "baseline": baseline["target"],
            "candidate": candidate["target"],
            "gates": [
                {
                    "metric": "quality.exact_match",
                    "direction": "higher_is_better",
                    "threshold": 1.0,
                }
            ],
        },
    }

    unresolved = json.loads(json.dumps(body))
    del unresolved["spec"]["dataset"]["digest"]
    unresolved_response = api_harness.client.post(
        "/v1/comparisons",
        json=unresolved,
        headers={"Idempotency-Key": "unresolved-comparison"},
    )
    created = api_harness.client.post(
        "/v1/comparisons",
        json=body,
        headers={"Idempotency-Key": "comparison-one"},
    )
    explicit_defaults = json.loads(json.dumps(body))
    explicit_defaults["spec"]["schema_version"] = "1"
    explicit_defaults["spec"]["gates"][0]["allowed_regression"] = 0.0
    replay = api_harness.client.post(
        "/v1/comparisons",
        json=explicit_defaults,
        headers={"Idempotency-Key": "comparison-one"},
    )

    assert unresolved_response.status_code == 422
    assert unresolved_response.json()["error"]["code"] == "invalid_request"
    assert created.status_code == replay.status_code == 202
    assert replay.json() == created.json()
    assert created.json()["schema_version"] == "comparison-submission/v2"
    assert created.json()["job"]["schema_version"] == "job/v2"
    assert created.json()["job"]["status"] == "queued"
    assert created.json()["decision"] is None
    assert api_harness.executor.calls == 0
    serialized = json.dumps(created.json(), sort_keys=True)
    for private_value in (
        "private-sentinel",
        "comparison-001",
        "idempotency_key",
        "request_digest",
        "payload_digest",
        "lease_token",
    ):
        assert private_value not in serialized


def test_release_decision_dashboard_reads_are_bounded_and_redacted(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
    run_body: dict[str, object],
) -> None:
    decision_id = _seed_release_decision(api_harness, dataset_body, run_body)

    listed = api_harness.client.get("/v1/release-decisions?limit=1&order=desc")
    cases = api_harness.client.get(
        f"/v1/release-decisions/{decision_id}/cases",
        params={
            "metric": "quality.exact_match",
            "case_slice": "core",
            "change": "unchanged_passing",
            "limit": 1,
        },
    )
    distributions = api_harness.client.get(
        f"/v1/release-decisions/{decision_id}/distributions",
        params={"metric": "quality.exact_match"},
    )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["decision_id"] == decision_id
    assert cases.status_code == 200
    assert cases.json() == {
        "schema_version": "release-decision-case-page/v1",
        "decision_id": decision_id,
        "items": [
            {
                "schema_version": "release-decision-case/v1",
                "case_id": "echo-001",
                "metric": "quality.exact_match",
                "gate_slice": None,
                "slices": ["core"],
                "baseline": {"status": "scored", "value": 1.0},
                "candidate": {"status": "scored", "value": 1.0},
                "delta": 0.0,
                "baseline_passed": True,
                "candidate_passed": True,
                "change": "unchanged_passing",
            }
        ],
        "next_cursor": None,
    }
    assert distributions.status_code == 200
    distribution_document = distributions.json()
    assert distribution_document["schema_version"] == (
        "release-decision-distributions/v1"
    )
    assert distribution_document["decision_id"] == decision_id
    assert distribution_document["score"]["metric"] == "quality.exact_match"
    assert distribution_document["score"]["baseline"]["attempted"] == 1
    assert distribution_document["score"]["baseline"]["scored"] == 1
    assert distribution_document["score"]["delta"]["compared"] == 1
    assert distribution_document["score"]["delta"]["statistics"]["small_sample"] is True
    for role in ("baseline", "candidate"):
        run = distribution_document[role]
        assert run["role"] == role
        assert run["execution_mode"] == "offline_mock"
        assert run["simulated"] is True
        assert run["latency_ms"]["measured"] == 1
        assert run["latency_ms"]["statistics"] == {
            "sample_count": 1,
            "small_sample": True,
            "suppressed": True,
            "minimum": None,
            "p50": None,
            "p95": None,
            "maximum": None,
            "mean": None,
        }
        assert run["input_units"]["measured"] == 1
        assert run["output_units"]["measured"] == 1
        assert run["total_units"]["measured"] == 1

    serialized = cases.text + distributions.text
    for forbidden in (
        "private-sentinel",
        "dashboard-baseline",
        "dashboard-candidate",
        "idempotency_key",
        "reason_code",
    ):
        assert forbidden not in serialized
    for forbidden in ("output", "expected", "target", "usage"):
        assert forbidden not in cases.text

    wrong_metric = api_harness.client.get(
        f"/v1/release-decisions/{decision_id}/cases",
        params={"metric": "private sentinel"},
    )
    assert wrong_metric.status_code == 422
    assert "private sentinel" not in wrong_metric.text

    missing_gate = api_harness.client.get(
        f"/v1/release-decisions/{decision_id}/distributions",
        params={"metric": "quality.missing"},
    )
    assert missing_gate.status_code == 404
    assert missing_gate.json()["error"]["code"] == "resource_not_found"


def test_idempotency_mismatch_and_missing_dataset_are_stable(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
    run_body: dict[str, object],
) -> None:
    missing = api_harness.client.post(
        "/v1/runs",
        json=run_body,
        headers={"Idempotency-Key": "missing-dataset"},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "resource_not_found"

    _create_dataset(api_harness, dataset_body)
    headers = {"Idempotency-Key": "same-key"}
    assert (
        api_harness.client.post("/v1/runs", json=run_body, headers=headers).status_code
        == 202
    )
    changed = api_harness.client.post(
        "/v1/runs",
        json={**run_body, "target_revision": 3},
        headers=headers,
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "idempotency_conflict"
    assert api_harness.executor.calls == 0


def test_job_lists_accept_canceled_and_reject_unknown_statuses(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
    run_body: dict[str, object],
) -> None:
    _create_dataset(api_harness, dataset_body)
    queued = _submit_run(api_harness, run_body, key="list-canceled")
    job_id = _object_part(queued, "job")["job_id"]
    assert isinstance(job_id, str)
    api_harness.client.post(f"/v1/jobs/{job_id}/cancellation", json={})

    canceled = api_harness.client.get("/v1/jobs?status=canceled&limit=1")
    unknown = api_harness.client.get("/v1/jobs?status=private-sentinel")
    bad_cursor = api_harness.client.get("/v1/jobs?cursor=not-a-valid-cursor")

    assert canceled.status_code == 200
    assert canceled.json()["schema_version"] == "job-page/v2"
    assert canceled.json()["items"][0]["status"] == "canceled"
    assert unknown.status_code == 422
    assert "private-sentinel" not in unknown.text
    assert bad_cursor.status_code == 400
    assert bad_cursor.json()["error"]["code"] == "invalid_cursor"


def test_health_requires_persistence_and_current_schema(
    api_harness: ApiHarness,
) -> None:
    assert api_harness.client.get("/health/live").json() == {
        "schema_version": "health/v1",
        "status": "ok",
    }
    assert api_harness.client.get("/health/ready").status_code == 200

    api_harness.repository.current_schema = False
    unavailable = api_harness.client.get("/health/ready")
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "schema_version": "health/v1",
        "status": "unavailable",
    }


def test_new_job_endpoints_redact_persistence_and_unexpected_errors(
    api_harness: ApiHarness,
    monkeypatch: MonkeyPatch,
) -> None:
    def unavailable(_job_id: str) -> Never:
        raise ControlPlaneStoreError("private-sentinel /var/private db-password")

    monkeypatch.setattr(api_harness.service, "list_job_attempts", unavailable)
    persistence = api_harness.client.get("/v1/jobs/job_001/attempts")
    assert persistence.status_code == 503
    assert persistence.json()["error"]["code"] == "persistence_unavailable"
    assert "private-sentinel" not in persistence.text
    assert "/var/private" not in persistence.text

    def explode(_job_id: str) -> Never:
        raise RuntimeError("private-sentinel api-key-value")

    monkeypatch.setattr(api_harness.service, "cancel_job", explode)
    unexpected = api_harness.client.post(
        "/v1/jobs/job_001/cancellation",
        json={},
    )
    assert unexpected.status_code == 500
    assert unexpected.json()["error"]["code"] == "internal_error"
    assert "private-sentinel" not in unexpected.text


def test_invalid_job_paths_fail_before_repository_access(
    api_harness: ApiHarness,
) -> None:
    for method, path, body in (
        ("get", "/v1/jobs/bad%20job", None),
        ("get", "/v1/jobs/bad%20job/attempts", None),
        ("post", "/v1/jobs/bad%20job/cancellation", {}),
        ("get", "/v1/runs/bad:run", None),
        ("get", "/v1/release-decisions/bad%20decision", None),
    ):
        response = api_harness.client.request(method, path, json=body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"
