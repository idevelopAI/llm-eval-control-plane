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
        "list_jobs",
        "list_release_decisions",
        "submit_evaluation_run",
        "submit_release_comparison",
    }
    run_responses = document["paths"]["/v1/runs"]["post"]["responses"]
    comparison_responses = document["paths"]["/v1/comparisons"]["post"]["responses"]
    assert {"200", "201", "202"} <= set(run_responses)
    assert {"200", "201", "202"} <= set(comparison_responses)
    for responses in (run_responses, comparison_responses):
        for status in ("200", "201", "202"):
            location = responses[status]["headers"]["Location"]
            assert location["schema"]["pattern"].startswith("^/v1/jobs/")
    assert "503" in document["paths"]["/health/ready"]["get"]["responses"]


def test_openapi_pins_versioned_redacted_response_contracts(
    api_harness: ApiHarness,
) -> None:
    schemas = api_harness.client.get("/openapi.json").json()["components"]["schemas"]

    expected_versions = {
        "ApiErrorDocument": "api-error/v1",
        "ComparisonSubmissionResponse": "comparison-submission/v1",
        "DatasetPage": "dataset-page/v1",
        "DatasetResponse": "dataset-summary/v1",
        "HealthResponse": "health/v1",
        "JobPage": "job-page/v1",
        "JobResponse": "job/v1",
        "ReleaseDecisionPage": "release-decision-page/v1",
        "ReleaseDecisionResponse": "release-decision-summary/v1",
        "RunPage": "run-page/v1",
        "RunResponse": "run-summary/v1",
        "RunSubmissionResponse": "run-submission/v1",
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
