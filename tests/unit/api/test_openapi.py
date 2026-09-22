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
        "create_suite_revision",
        "get_dataset_revision",
        "get_evaluation_run",
        "get_job",
        "get_liveness",
        "get_readiness",
        "get_release_decision",
        "get_release_decision_distributions",
        "get_suite_revision",
        "list_dataset_revisions",
        "list_evaluation_runs",
        "list_job_attempts",
        "list_jobs",
        "list_release_decisions",
        "list_release_decision_cases",
        "list_suite_revisions",
        "list_suite_run_history",
        "list_suite_decision_history",
        "list_suite_target_groups",
        "list_suite_target_pair_groups",
        "request_job_cancellation",
        "submit_evaluation_run",
        "submit_release_comparison",
        "submit_suite_evaluation_run",
        "submit_suite_release_comparison",
    }
    for path in (
        "/v1/runs",
        "/v1/comparisons",
        "/v1/suite-runs",
        "/v1/suite-comparisons",
    ):
        responses = document["paths"][path]["post"]["responses"]
        assert {"200", "202"} <= set(responses)
        assert "201" not in responses
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
        "ReleaseDecisionCasePage": "release-decision-case-page/v1",
        "ReleaseDecisionCaseResponse": "release-decision-case/v1",
        "ReleaseDecisionDistributionsResponse": ("release-decision-distributions/v1"),
        "ReleaseDecisionPage": "release-decision-page/v1",
        "ReleaseDecisionResponse": "release-decision-summary/v1",
        "RunListItemResponse": "run-list-item/v1",
        "RunPage": "run-page/v1",
        "RunResponse": "run-summary/v1",
        "RunSubmissionResponse": "run-submission/v2",
        "SuiteResponse": "suite-summary/v1",
        "SuiteListItemResponse": "suite-list-item/v1",
        "SuitePage": "suite-page/v1",
        "SuiteRunHistoryItemResponse": "suite-run-history-item/v1",
        "SuiteRunHistoryPage": "suite-run-history-page/v1",
        "SuiteDecisionHistoryItemResponse": "suite-decision-history-item/v1",
        "SuiteDecisionHistoryPage": "suite-decision-history-page/v1",
        "SuiteTargetGroupResponse": "suite-target-group/v1",
        "SuiteTargetGroupPage": "suite-target-group-page/v1",
        "SuiteTargetPairGroupResponse": "suite-target-pair-group/v1",
        "SuiteTargetPairGroupPage": "suite-target-pair-group-page/v1",
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
    for name in ("RunResponse", "ReleaseDecisionResponse"):
        assert "suite" in schemas[name]["properties"]
        assert "suite" not in schemas[name]["required"]
    for name in ("SuiteResponse", "SuiteListItemResponse"):
        assert (
            not {
                "document",
                "cases",
                "expected",
                "input",
                "output",
                "scenario_overrides",
                "target",
            }
            & schemas[name]["properties"].keys()
        )
    assert (
        not {"evaluators", "execution", "gates", "slices"}
        & schemas["SuiteListItemResponse"]["properties"].keys()
    )

    decision_case_properties = schemas["ReleaseDecisionCaseResponse"]["properties"]
    assert {
        "baseline",
        "baseline_passed",
        "candidate",
        "candidate_passed",
        "case_id",
        "change",
        "delta",
        "gate_slice",
        "metric",
        "slices",
    } <= set(decision_case_properties)
    for forbidden in (
        "expected",
        "input",
        "message",
        "output",
        "reason_code",
        "sql",
        "target",
        "usage",
    ):
        assert forbidden not in decision_case_properties

    distribution_properties = schemas["ReleaseDecisionDistributionsResponse"][
        "properties"
    ]
    assert {"baseline", "candidate", "decision_id", "score"} <= set(
        distribution_properties
    )
    distribution_schemas = {
        name: schema
        for name, schema in schemas.items()
        if "Distribution" in name or name == "QuantileSummaryResponse"
    }

    def property_names(value: object) -> set[str]:
        if isinstance(value, list):
            return set().union(*(property_names(item) for item in value))
        if not isinstance(value, dict):
            return set()
        names: set[str] = set()
        properties = value.get("properties")
        if isinstance(properties, dict):
            names.update(str(name) for name in properties)
        for item in value.values():
            names.update(property_names(item))
        return names

    exposed_distribution_names = property_names(distribution_schemas)
    for forbidden in (
        "case_id",
        "expected",
        "input",
        "message",
        "output",
        "raw_samples",
        "reason_code",
        "sql",
    ):
        assert forbidden not in exposed_distribution_names

    paths = api_harness.client.get("/openapi.json").json()["paths"]
    case_parameters = paths["/v1/release-decisions/{decision_id}/cases"]["get"][
        "parameters"
    ]
    gate_slice = next(item for item in case_parameters if item["name"] == "gate_slice")
    assert "omission selects the global gate" in gate_slice["description"]

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
    for path in (
        "/v1/runs",
        "/v1/comparisons",
        "/v1/suite-runs",
        "/v1/suite-comparisons",
    ):
        operation = document["paths"][path]["post"]
        header = next(
            parameter
            for parameter in operation["parameters"]
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
    schemas = document["components"]["schemas"]
    suite = schemas["SuiteCreateRequest"]
    assert suite["additionalProperties"] is False
    for field, bound in (("evaluators", 32), ("slices", 128), ("gates", 64)):
        assert suite["properties"][field]["maxItems"] == bound
    for name in ("SuiteRunCreateRequest", "SuiteComparisonCreateRequest"):
        assert schemas[name]["additionalProperties"] is False
        assert (
            not {"dataset", "execution", "evaluators", "gates", "spec"}
            & schemas[name]["properties"].keys()
        )


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
