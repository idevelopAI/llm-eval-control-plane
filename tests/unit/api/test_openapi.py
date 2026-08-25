from typing import cast

from fastapi import FastAPI

from .conftest import ApiHarness


def test_openapi_operation_ids_and_dynamic_responses_are_stable(
    api_harness: ApiHarness,
) -> None:
    document = api_harness.client.get("/openapi.json").json()
    operation_ids = {
        operation["operationId"]
        for path in document["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    }

    assert operation_ids == {
        "create_dataset_revision",
        "get_dataset_revision",
        "get_evaluation_run",
        "get_job",
        "get_liveness",
        "get_readiness",
        "get_release_decision",
        "list_dataset_revisions",
        "list_evaluation_runs",
        "list_job_attempts",
        "list_jobs",
        "list_release_decisions",
        "request_job_cancellation",
        "submit_evaluation_run",
        "submit_release_comparison",
    }
    run_responses = document["paths"]["/v1/runs"]["post"]["responses"]
    comparison_responses = document["paths"]["/v1/comparisons"]["post"]["responses"]
    assert {"200", "202"} <= set(run_responses)
    assert {"200", "202"} <= set(comparison_responses)
    assert "201" not in run_responses
    assert "201" not in comparison_responses
    for responses in (run_responses, comparison_responses):
        for status in ("200", "202"):
            location = responses[status]["headers"]["Location"]
            assert location["schema"]["pattern"].startswith("^/v1/jobs/")
    assert "503" in document["paths"]["/health/ready"]["get"]["responses"]


def test_openapi_pins_versioned_redacted_response_contracts(
    api_harness: ApiHarness,
) -> None:
    schemas = api_harness.client.get("/openapi.json").json()["components"]["schemas"]

    expected_versions = {
        "ApiErrorDocument": "api-error/v1",
        "ComparisonSubmissionResponse": "comparison-submission/v2",
        "DatasetListItemResponse": "dataset-list-item/v1",
        "DatasetPage": "dataset-page/v1",
        "DatasetResponse": "dataset-summary/v1",
        "HealthResponse": "health/v1",
        "JobAttemptListResponse": "job-attempt-list/v1",
        "JobAttemptResponse": "job-attempt/v1",
        "JobPage": "job-page/v2",
        "JobResponse": "job/v2",
        "ReleaseDecisionListItemResponse": "release-decision-list-item/v1",
        "ReleaseDecisionPage": "release-decision-page/v1",
        "ReleaseDecisionResponse": "release-decision-summary/v1",
        "RunListItemResponse": "run-list-item/v1",
        "RunPage": "run-page/v1",
        "RunResponse": "run-summary/v1",
        "RunSubmissionResponse": "run-submission/v2",
    }
    for name, version in expected_versions.items():
        assert schemas[name]["properties"]["schema_version"]["const"] == version

    run_properties = schemas["RunResponse"]["properties"]
    decision_properties = schemas["ReleaseDecisionResponse"]["properties"]
    assert "cases" not in run_properties
    assert "case_status_counts" in run_properties
    assert "cases" not in decision_properties
    assert "baseline_result_digest" in decision_properties
    assert "candidate_result_digest" in decision_properties

    job_properties = schemas["JobResponse"]["properties"]
    attempt_properties = schemas["JobAttemptResponse"]["properties"]
    assert {"attempt_count", "max_attempts", "available_at"} <= set(job_properties)
    for private_field in (
        "idempotency_key",
        "request_digest",
        "payload",
        "worker_id",
        "lease_token",
    ):
        assert private_field not in job_properties
        assert private_field not in attempt_properties

    run_list_properties = schemas["RunListItemResponse"]["properties"]
    decision_list_properties = schemas["ReleaseDecisionListItemResponse"]["properties"]
    assert "metrics" not in run_list_properties
    assert "case_status_counts" not in run_list_properties
    assert "aggregates" not in decision_list_properties
    assert "gates" not in decision_list_properties


def test_openapi_documents_required_idempotency_header_and_bounds(
    api_harness: ApiHarness,
) -> None:
    document = api_harness.client.get("/openapi.json").json()
    run = document["paths"]["/v1/runs"]["post"]
    header = next(
        parameter
        for parameter in run["parameters"]
        if parameter["name"] == "Idempotency-Key"
    )

    assert header["required"] is True
    assert header["in"] == "header"
    assert header["schema"]["maxLength"] == 128
    assert (
        document["components"]["schemas"]["DatasetCreateRequest"]["properties"][
            "cases"
        ]["maxItems"]
        == 1_000
    )
    assert (
        document["components"]["schemas"]["EvaluationSpecInput"]["properties"]["gates"][
            "maxItems"
        ]
        == 64
    )
    resolved_reference = document["components"]["schemas"]["ResolvedArtifactRefInput"]
    assert "digest" in resolved_reference["required"]


def test_openapi_applies_project_bearer_security_only_to_v1_operations(
    api_harness: ApiHarness,
) -> None:
    document = api_harness.client.get("/openapi.json").json()

    assert document["components"]["securitySchemes"]["ProjectBearer"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "cpk_<base64url>",
        "description": "Project-bound opaque API credential",
    }
    assert "/metrics" not in document["paths"]
    for path, path_item in document["paths"].items():
        for operation in path_item.values():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            if path.startswith("/v1"):
                assert operation["security"] == [{"ProjectBearer": []}]
                project_headers = [
                    parameter
                    for parameter in operation.get("parameters", [])
                    if parameter.get("name") == "X-Project-ID"
                ]
                assert len(project_headers) == 1
                assert project_headers[0]["required"] is True
            else:
                assert "security" not in operation


def test_openapi_security_decoration_is_stable_across_repeat_calls(
    api_harness: ApiHarness,
) -> None:
    app = cast(FastAPI, api_harness.client.app)
    first = app.openapi()
    second = app.openapi()
    third = api_harness.client.get("/openapi.json").json()

    assert first is second
    assert first == third
    for path, path_item in first["paths"].items():
        if not path.startswith("/v1"):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            project_headers = [
                parameter
                for parameter in operation.get("parameters", [])
                if parameter.get("name") == "X-Project-ID"
            ]
            assert len(project_headers) == 1


def test_interactive_documentation_does_not_load_third_party_assets(
    api_harness: ApiHarness,
) -> None:
    responses = tuple(
        api_harness.client.get(path)
        for path in ("/docs", "/docs/oauth2-redirect", "/redoc")
    )

    assert all(response.status_code == 404 for response in responses)
    rendered = "".join(response.text for response in responses)
    assert "<script" not in rendered.casefold()
    assert "cdn.jsdelivr.net" not in rendered.casefold()
