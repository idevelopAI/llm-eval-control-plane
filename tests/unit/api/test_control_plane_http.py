import json
from datetime import UTC, datetime
from typing import Never

from pytest import MonkeyPatch

from llm_eval_control_plane.application.control_plane import (
    ControlPlaneStoreError,
    RunSubmission,
)
from llm_eval_control_plane.domain import sha256_digest
from llm_eval_control_plane.domain.control_plane import JobKind, JobRecord, JobStatus

from .conftest import ApiHarness


def _create_dataset(
    harness: ApiHarness,
    dataset_body: dict[str, object],
) -> None:
    response = harness.client.post("/v1/datasets", json=dataset_body)
    assert response.status_code == 201


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
        "created_at": "2026-08-20T12:00:00Z",
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
    cases = changed["cases"]
    assert isinstance(cases, list)
    first_case = cases[0]
    assert isinstance(first_case, dict)
    first_case["expected"] = "private-changed-value"
    conflict = api_harness.client.post("/v1/datasets", json=changed)

    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "resource_conflict"
    assert "private-changed-value" not in conflict.text


def test_run_submission_is_durable_idempotent_and_redacted(
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
    explicit_defaults = {
        **run_body,
        "adapter": "deterministic_fake",
        "scenario_overrides": {},
    }
    replay = api_harness.client.post(
        "/v1/runs",
        json=explicit_defaults,
        headers={"Idempotency-Key": "run-request-001"},
    )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert first.headers["location"] == f"/v1/jobs/{first.json()['job']['job_id']}"
    assert api_harness.executor.calls == 1
    document = first.json()
    assert document["schema_version"] == "run-submission/v1"
    assert document["job"]["schema_version"] == "job/v1"
    assert document["job"]["status"] == "succeeded"
    assert document["run"]["schema_version"] == "run-summary/v1"
    assert document["run"]["case_status_counts"] == {
        "completed": 1,
        "completed_with_errors": 0,
        "target_failed": 0,
    }
    serialized = json.dumps(document, sort_keys=True)
    for private_value in (
        "private-sentinel",
        "run-request-001",
        "idempotency_key",
        "request_digest",
        '"cases"',
        '"input"',
        '"output"',
        '"expected"',
    ):
        assert private_value not in serialized

    loaded = api_harness.client.get(f"/v1/runs/{document['run']['run_id']}")
    assert loaded.json() == document["run"]


def test_queued_and_running_replays_return_accepted_without_execution(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
    run_body: dict[str, object],
) -> None:
    _create_dataset(api_harness, dataset_body)
    submission = RunSubmission(
        idempotency_key="stranded-request",
        dataset_name="release-gate/offline",
        dataset_revision=1,
        target_name="fake/candidate",
        target_revision=2,
        adapter="deterministic_fake",
        evaluator_names=("exact_match",),
        scenario_overrides={},
    )
    now = datetime(2026, 8, 20, 12, tzinfo=UTC)
    queued = JobRecord(
        job_id="stranded-job",
        kind=JobKind.RUN,
        status=JobStatus.QUEUED,
        idempotency_key=submission.idempotency_key,
        request_digest=sha256_digest(submission.digest_record()),
        resource_id="stranded-run",
        created_at=now,
        updated_at=now,
    )
    api_harness.repository.begin_job(queued)

    first = api_harness.client.post(
        "/v1/runs",
        json=run_body,
        headers={"Idempotency-Key": "stranded-request"},
    )
    api_harness.repository.transition_job(
        "stranded-job",
        JobStatus.RUNNING,
        at=now,
    )
    second = api_harness.client.post(
        "/v1/runs",
        json=run_body,
        headers={"Idempotency-Key": "stranded-request"},
    )

    assert first.status_code == second.status_code == 202
    assert first.json()["job"]["status"] == "queued"
    assert second.json()["job"]["status"] == "running"
    assert first.json()["run"] is second.json()["run"] is None
    assert api_harness.executor.calls == 0


def test_comparison_submission_persists_redacted_decision_and_replays(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
    run_body: dict[str, object],
) -> None:
    _create_dataset(api_harness, dataset_body)
    baseline_response = api_harness.client.post(
        "/v1/runs",
        json={**run_body, "target_name": "fake/baseline", "target_revision": 1},
        headers={"Idempotency-Key": "baseline-run"},
    )
    candidate_response = api_harness.client.post(
        "/v1/runs",
        json={**run_body, "target_name": "fake/candidate", "target_revision": 2},
        headers={"Idempotency-Key": "candidate-run"},
    )
    baseline = baseline_response.json()["run"]
    candidate = candidate_response.json()["run"]
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
    assert unresolved_response.status_code == 422
    assert unresolved_response.json()["error"]["code"] == "invalid_request"

    created = api_harness.client.post(
        "/v1/comparisons",
        json=body,
        headers={"Idempotency-Key": "comparison-001"},
    )
    explicit_defaults = json.loads(json.dumps(body))
    explicit_defaults["spec"]["schema_version"] = "1"
    explicit_defaults["spec"]["gates"][0]["allowed_regression"] = 0.0
    replay = api_harness.client.post(
        "/v1/comparisons",
        json=explicit_defaults,
        headers={"Idempotency-Key": "comparison-001"},
    )

    assert created.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == created.json()
    assert api_harness.executor.calls == 2
    document = created.json()
    assert document["schema_version"] == "comparison-submission/v1"
    assert document["job"]["status"] == "succeeded"
    assert document["decision"]["schema_version"] == ("release-decision-summary/v1")
    assert document["decision"]["status"] == "passed"
    assert document["decision"]["baseline_result_digest"] == baseline["result_digest"]
    assert document["decision"]["candidate_result_digest"] == candidate["result_digest"]
    assert created.headers["location"] == f"/v1/jobs/{document['job']['job_id']}"
    serialized = json.dumps(document, sort_keys=True)
    for private_value in (
        "private-sentinel",
        "comparison-001",
        "idempotency_key",
        "request_digest",
        '"cases"',
        '"input"',
        '"output"',
        '"expected"',
    ):
        assert private_value not in serialized

    decision_id = document["decision"]["decision_id"]
    loaded = api_harness.client.get(f"/v1/release-decisions/{decision_id}")
    listed = api_harness.client.get("/v1/release-decisions?status=passed")
    assert loaded.json() == document["decision"]
    assert listed.json()["schema_version"] == "release-decision-page/v1"
    assert listed.json()["items"] == [
        {
            "schema_version": "release-decision-list-item/v1",
            "decision_id": decision_id,
            "status": "passed",
            "baseline_run_id": baseline["run_id"],
            "candidate_run_id": candidate["run_id"],
            "decision_digest": document["decision"]["decision_digest"],
            "created_at": "2026-08-20T12:00:00Z",
        }
    ]

    failed_body = json.loads(json.dumps(body))
    failed_body["spec"]["name"] = "invalid-gate-policy"
    failed_body["spec"]["gates"][0]["metric"] = "quality.absent"
    failed = api_harness.client.post(
        "/v1/comparisons",
        json=failed_body,
        headers={"Idempotency-Key": "failed-comparison"},
    )
    assert failed.status_code == 201
    assert failed.json()["job"]["status"] == "failed"
    assert failed.json()["job"]["error_code"] == "comparison_failed"
    assert failed.json()["decision"] is None


def test_idempotency_key_body_mismatch_returns_stable_conflict(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
    run_body: dict[str, object],
) -> None:
    _create_dataset(api_harness, dataset_body)
    headers = {"Idempotency-Key": "same-key"}
    assert api_harness.client.post(
        "/v1/runs", json=run_body, headers=headers
    ).is_success
    changed = {**run_body, "target_revision": 3}

    response = api_harness.client.post("/v1/runs", json=changed, headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"
    assert api_harness.executor.calls == 1


def test_missing_dataset_is_rejected_before_job_creation(
    api_harness: ApiHarness,
    run_body: dict[str, object],
) -> None:
    response = api_harness.client.post(
        "/v1/runs",
        json=run_body,
        headers={"Idempotency-Key": "missing-dataset"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"
    assert api_harness.executor.calls == 0
    jobs = api_harness.client.get("/v1/jobs").json()
    assert jobs["items"] == []


def test_job_and_run_lists_validate_filters_and_cursors(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
    run_body: dict[str, object],
) -> None:
    _create_dataset(api_harness, dataset_body)
    api_harness.client.post(
        "/v1/runs",
        json=run_body,
        headers={"Idempotency-Key": "list-run"},
    )

    jobs = api_harness.client.get("/v1/jobs?kind=run&status=succeeded&limit=1")
    runs = api_harness.client.get("/v1/runs?dataset_name=release-gate/offline&limit=1")
    bad_enum = api_harness.client.get("/v1/jobs?status=private-sentinel")
    bad_cursor = api_harness.client.get("/v1/runs?cursor=not-a-valid-cursor")

    assert jobs.status_code == runs.status_code == 200
    assert jobs.json()["schema_version"] == "job-page/v1"
    assert runs.json()["schema_version"] == "run-page/v1"
    run_item = runs.json()["items"][0]
    assert run_item["schema_version"] == "run-list-item/v1"
    assert run_item["dataset_name"] == "release-gate/offline"
    assert "metrics" not in run_item
    assert "case_status_counts" not in run_item
    assert bad_enum.status_code == 422
    assert "private-sentinel" not in bad_enum.text
    assert bad_cursor.status_code == 400
    assert bad_cursor.json()["error"]["code"] == "invalid_cursor"


def test_health_requires_persistence_and_current_schema(
    api_harness: ApiHarness,
    monkeypatch: MonkeyPatch,
) -> None:
    assert api_harness.client.get("/health/live").json() == {
        "schema_version": "health/v1",
        "status": "ok",
    }
    assert api_harness.client.get("/health/ready").status_code == 200

    monkeypatch.setattr(api_harness.repository, "schema_is_current", lambda: False)
    unavailable = api_harness.client.get("/health/ready")

    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "schema_version": "health/v1",
        "status": "unavailable",
    }


def test_persistence_and_unexpected_errors_never_echo_exception_details(
    api_harness: ApiHarness,
    monkeypatch: MonkeyPatch,
) -> None:
    def unavailable(_job_id: str) -> Never:
        raise ControlPlaneStoreError("private-sentinel /var/private db-password")

    monkeypatch.setattr(api_harness.service, "get_job", unavailable)
    persistence = api_harness.client.get("/v1/jobs/job_001")

    assert persistence.status_code == 503
    assert persistence.json()["error"]["code"] == "persistence_unavailable"
    assert "private-sentinel" not in persistence.text
    assert "/var/private" not in persistence.text

    def explode(_job_id: str) -> Never:
        raise RuntimeError("private-sentinel api-key-value")

    monkeypatch.setattr(api_harness.service, "get_job", explode)
    unexpected = api_harness.client.get("/v1/jobs/job_001")

    assert unexpected.status_code == 500
    assert unexpected.json()["error"]["code"] == "internal_error"
    assert "private-sentinel" not in unexpected.text


def test_invalid_identifier_paths_fail_before_repository_access(
    api_harness: ApiHarness,
) -> None:
    for path in (
        "/v1/jobs/bad%20job",
        "/v1/runs/bad:run",
        "/v1/release-decisions/bad%20decision",
    ):
        response = api_harness.client.get(path)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"
