from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Never, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.telemetry import _runtime as fastapi_telemetry_runtime
from fastapi.testclient import TestClient
from opentelemetry import _logs, metrics, trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pytest import MonkeyPatch

from llm_eval_control_plane.api.app import create_app
from llm_eval_control_plane.api.middleware import ApiBoundaryMiddleware
from llm_eval_control_plane.api.observability import (
    ApiObservabilityMiddleware,
    set_error_code,
)
from llm_eval_control_plane.observability import Observability

from .conftest import AUTH_HEADERS, ApiHarness, build_authorizer

NOW = datetime(2026, 8, 25, 12, 34, 56, 789_000, tzinfo=UTC)
TRACE_ID = "1234567890abcdef1234567890abcdef"
PARENT_SPAN_ID = "1234567890abcdef"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01"


class StepClock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        self.value += 5_000_000
        return self.value


def _telemetry() -> tuple[
    Observability,
    list[str],
    InMemorySpanExporter,
    TracerProvider,
]:
    lines: list[str] = []
    exporter = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource(
            {
                "service.name": "llm-eval-control-plane-test",
                "service.version": "test",
            }
        )
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = Observability(
        service="api",
        version="test",
        tracer_provider=provider,
        log_sink=lines.append,
        clock_ns=StepClock(),
        wall_clock=lambda: NOW,
    )
    return telemetry, lines, exporter, provider


def _app(
    telemetry: Observability,
    *,
    fail: bool = False,
) -> FastAPI:
    app = FastAPI()

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str, request: Request) -> JSONResponse:
        del job_id
        if fail:
            raise RuntimeError(
                "private-exception-sentinel https://user:secret@example.test/path"
            )
        if request.query_params.get("fail") == "safe":
            set_error_code(request.scope, "resource_not_found")
            return JSONResponse(status_code=404, content={"status": "not-found"})
        return JSONResponse(content={"status": "ok"})

    @app.post("/v1/jobs/{job_id}/cancellation")
    async def cancel_job(job_id: str, request: Request) -> JSONResponse:
        del job_id
        await request.json()
        return JSONResponse(content={"status": "ok"})

    app.add_middleware(
        ApiBoundaryMiddleware,
        max_body_bytes=4_096,
        authorizer=build_authorizer(),
    )
    app.add_middleware(ApiObservabilityMiddleware, telemetry=telemetry)
    return app


def _finished(exporter: InMemorySpanExporter) -> tuple[ReadableSpan, ...]:
    return tuple(exporter.get_finished_spans())


def test_production_app_does_not_export_native_framework_telemetry(
    api_harness: ApiHarness,
    monkeypatch: MonkeyPatch,
) -> None:
    """Global providers and exporter opt-in cannot bypass the safe envelope."""
    telemetry, lines, safe_spans, safe_provider = _telemetry()
    global_spans = InMemorySpanExporter()
    global_tracer = TracerProvider(resource=Resource({}))
    global_tracer.add_span_processor(SimpleSpanProcessor(global_spans))
    # The SDK's in-memory exporter omits only its constructor's return annotation.
    global_logs = InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]
    global_logger = LoggerProvider(resource=Resource({}))
    global_logger.add_log_record_processor(SimpleLogRecordProcessor(global_logs))
    global_reader = InMemoryMetricReader()
    global_meter = MeterProvider(resource=Resource({}), metric_readers=[global_reader])
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: global_tracer)
    monkeypatch.setattr(_logs, "get_logger_provider", lambda: global_logger)
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: global_meter)
    monkeypatch.setenv("FASTAPI_OTEL_AUTO_CONFIGURE", "true")
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    exporter_lookups: list[str] = []

    def capture_exporter_lookup(signal: str) -> None:
        # Trap automatic setup before any exporter or network client is created.
        exporter_lookups.append(signal)

    def fail_get_job(_job_id: str) -> Never:
        raise RuntimeError("private-exception-sentinel")

    monkeypatch.setattr(
        fastapi_telemetry_runtime, "_export_endpoint", capture_exporter_lookup
    )
    monkeypatch.setattr(api_harness.repository, "get_job", fail_get_job)
    app = create_app(
        service=api_harness.service,
        authorizer=build_authorizer(),
        telemetry=telemetry,
    )
    try:
        with TestClient(
            app, headers=AUTH_HEADERS, raise_server_exceptions=False
        ) as client:
            healthy = client.get("/health/live")
            invalid = client.post(
                "/v1/datasets?token=private-query-sentinel",
                json={"name": "private-body-sentinel", "revision": "not-an-integer"},
            )
            failed = client.get("/v1/jobs/private-path-sentinel")

        assert [healthy.status_code, invalid.status_code, failed.status_code] == [
            200,
            422,
            500,
        ]
        assert global_spans.get_finished_spans() == ()
        assert global_logs.get_finished_logs() == ()
        assert global_reader.get_metrics_data() is None
        assert exporter_lookups == []
        # Operation spans must stay off even if a future change enables tracing.
        assert app._telemetry["operation_spans"] is False
        assert app._telemetry["auto_configure"] is False
        events = [json.loads(line) for line in lines]
        assert [event["http_status"] for event in events] == [200, 422, 500]
        assert events[-1]["error_code"] == "internal_error"
        spans = _finished(safe_spans)
        assert len(spans) == 3
        assert all(not span.events for span in spans)
        captured = (
            "".join(lines)
            + telemetry.render_metrics().body.decode()
            + "".join(span.to_json() for span in spans)
        )
        for sentinel in (
            "private-query-sentinel",
            "private-body-sentinel",
            "private-path-sentinel",
            "private-exception-sentinel",
        ):
            assert sentinel not in captured
    finally:
        safe_provider.shutdown()
        global_tracer.shutdown()
        global_logger.shutdown()
        global_meter.shutdown()


def test_request_telemetry_uses_only_route_template_and_allowlisted_fields() -> None:
    telemetry, lines, exporter, provider = _telemetry()
    client = TestClient(
        _app(telemetry), headers=AUTH_HEADERS, raise_server_exceptions=False
    )
    caller_value = "cpk_" + ("C" * 43)
    sentinels = (
        "private-path-sentinel",
        "private-query-sentinel",
        "private-body-sentinel",
        "private-auth-sentinel",
        "private-baggage-sentinel",
        "private-tracestate-sentinel",
        caller_value,
    )

    response = client.post(
        "/v1/jobs/private-path-sentinel/cancellation?cursor=private-query-sentinel",
        json={"value": "private-body-sentinel"},
        headers={
            "Baggage": "customer=private-baggage-sentinel",
            "X-Credential-Probe": "private-auth-sentinel",
            "Traceparent": TRACEPARENT,
            "Tracestate": "vendor=private-tracestate-sentinel",
            "X-Request-ID": caller_value,
        },
    )

    assert response.status_code == 200
    request_id = response.headers["x-request-id"]
    assert request_id.startswith("req_") and len(request_id) == 36
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event == {
        "duration_ms": 5,
        "event": "http.request.completed",
        "http_method": "POST",
        "http_route": "/v1/jobs/{job_id}/cancellation",
        "http_status": 200,
        "outcome": "success",
        "request_id": request_id,
        "schema_version": "control-plane-log/v1",
        "service": "api",
        "severity": "INFO",
        "span_id": event["span_id"],
        "timestamp": "2026-08-25T12:34:56.789Z",
        "trace_id": TRACE_ID,
    }
    assert isinstance(event["span_id"], str) and len(event["span_id"]) == 16

    spans = _finished(exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "POST /v1/jobs/{job_id}/cancellation"
    assert span.context is not None
    assert f"{span.context.trace_id:032x}" == TRACE_ID
    assert span.parent is not None
    assert f"{span.parent.span_id:016x}" == PARENT_SPAN_ID
    assert dict(span.attributes or {}) == {
        "control_plane.request_id": request_id,
        "http.request.method": "POST",
        "http.response.status_code": 200,
        "http.route": "/v1/jobs/{job_id}/cancellation",
    }
    assert span.events == ()

    metrics = telemetry.render_metrics()
    assert metrics.content_type.startswith("text/plain")
    observed = (
        lines[0]
        + metrics.body.decode("utf-8")
        + json.dumps(
            {
                "attributes": dict(span.attributes or {}),
                "events": [],
                "name": span.name,
            },
            sort_keys=True,
        )
    )
    for sentinel in sentinels:
        assert sentinel not in observed
    assert "python_gc" not in observed
    assert "process_" not in observed
    provider.shutdown()


def test_errors_are_bounded_and_never_record_exception_details() -> None:
    telemetry, lines, exporter, provider = _telemetry()
    client = TestClient(
        _app(telemetry, fail=True),
        headers=AUTH_HEADERS,
        raise_server_exceptions=False,
    )

    response = client.get(
        "/v1/jobs/private-error-path?token=private-query-token",
        headers={"X-Request-ID": "error-request"},
    )

    assert response.status_code == 500
    event = json.loads(lines[0])
    request_id = event["request_id"]
    assert isinstance(request_id, str) and request_id.startswith("req_")
    assert event["http_route"] == "/v1/jobs/{job_id}"
    assert event["error_code"] == "internal_error"
    assert event["severity"] == "ERROR"
    assert event["outcome"] == "server_error"
    span = _finished(exporter)[0]
    assert dict(span.attributes or {}) == {
        "control_plane.error_code": "internal_error",
        "control_plane.request_id": request_id,
        "http.request.method": "GET",
        "http.response.status_code": 500,
        "http.route": "/v1/jobs/{job_id}",
    }
    assert span.events == ()
    assert span.status.status_code.name == "ERROR"

    captured = (
        lines[0]
        + telemetry.render_metrics().body.decode()
        + json.dumps(dict(span.attributes or {}))
    )
    for sentinel in (
        "private-exception-sentinel",
        "private-error-path",
        "private-query-token",
        "user:secret",
        "RuntimeError",
        "example.test",
        "error-request",
    ):
        assert sentinel not in captured
    provider.shutdown()


def test_unmatched_targets_and_error_codes_collapse_to_fixed_labels() -> None:
    telemetry, lines, exporter, provider = _telemetry()
    client = TestClient(
        _app(telemetry), headers=AUTH_HEADERS, raise_server_exceptions=False
    )

    missing = client.get(
        "/private-unmatched-sentinel?cursor=private-cursor-sentinel",
        headers={"X-Request-ID": "unmatched-request"},
    )
    known_error = client.get(
        "/v1/jobs/private-known-error?fail=safe",
        headers={"X-Request-ID": "known-error-request"},
    )

    assert missing.status_code == known_error.status_code == 404
    events = tuple(json.loads(line) for line in lines)
    assert events[0]["http_route"] == "unmatched"
    assert events[0]["error_code"] == "unknown"
    assert events[1]["http_route"] == "/v1/jobs/{job_id}"
    assert events[1]["error_code"] == "resource_not_found"
    assert {span.name for span in _finished(exporter)} == {
        "GET unmatched",
        "GET /v1/jobs/{job_id}",
    }
    metrics = telemetry.render_metrics().body.decode()
    assert (
        'control_plane_http_errors_total{code="unknown",route="unmatched"} 1.0'
        in metrics
    )
    assert (
        "control_plane_http_errors_total"
        '{code="resource_not_found",route="/v1/jobs/{job_id}"} 1.0' in metrics
    )
    assert "private-unmatched-sentinel" not in metrics + "".join(lines)
    assert "private-cursor-sentinel" not in metrics + "".join(lines)
    provider.shutdown()


def test_distinct_resource_ids_do_not_increase_metric_label_cardinality() -> None:
    telemetry, lines, exporter, provider = _telemetry()
    client = TestClient(
        _app(telemetry), headers=AUTH_HEADERS, raise_server_exceptions=False
    )

    for index in range(25):
        response = client.get(
            f"/v1/jobs/private-job-{index}?cursor=private-cursor-{index}",
            headers={"X-Request-ID": f"cardinality-{index}"},
        )
        assert response.status_code == 200

    metrics = telemetry.render_metrics().body.decode()
    request_samples = [
        line
        for line in metrics.splitlines()
        if line.startswith("control_plane_http_requests_total{")
    ]
    assert request_samples == [
        "control_plane_http_requests_total"
        '{method="GET",route="/v1/jobs/{job_id}",status_class="2xx"} 25.0'
    ]
    assert len(lines) == len(_finished(exporter)) == 25
    for index in range(25):
        assert f"private-job-{index}" not in metrics + "".join(lines)
        assert f"private-cursor-{index}" not in metrics + "".join(lines)
    provider.shutdown()


def test_invalid_trace_context_and_auth_values_are_not_propagated() -> None:
    telemetry, lines, exporter, provider = _telemetry()
    ambient_provider = TracerProvider()
    client = TestClient(
        _app(telemetry), headers=AUTH_HEADERS, raise_server_exceptions=False
    )
    uppercase_trace = TRACEPARENT.upper()

    with ambient_provider.get_tracer("private-ambient-provider").start_as_current_span(
        "private-ambient-span"
    ) as ambient_span:
        ambient_context = ambient_span.get_span_context()
        response = client.get(
            "/v1/jobs/safe-job",
            headers={
                "Baggage": "private-baggage=private-value",
                "Traceparent": uppercase_trace,
                "Tracestate": "private-vendor=private-state",
                "X-Request-ID": "trace-request",
            },
        )
    telemetry.record_auth_decision("private-auth-outcome")

    assert response.status_code == 200
    span = _finished(exporter)[0]
    assert span.context is not None
    assert f"{span.context.trace_id:032x}" != TRACE_ID
    assert span.context.trace_id != ambient_context.trace_id
    assert span.parent is None
    metrics = telemetry.render_metrics().body.decode()
    assert 'control_plane_auth_decisions_total{outcome="other"} 1.0' in metrics
    captured = metrics + "".join(lines) + json.dumps(dict(span.attributes or {}))
    for sentinel in (
        uppercase_trace,
        "private-baggage",
        "private-value",
        "private-vendor",
        "private-state",
        "private-auth-outcome",
        "private-ambient-provider",
        "private-ambient-span",
    ):
        assert sentinel not in captured
    ambient_provider.shutdown()
    provider.shutdown()


def test_duplicate_traceparent_is_ignored_and_log_sink_failures_are_isolated() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def broken_sink(_document: str) -> None:
        raise RuntimeError("private-sink-failure")

    telemetry = Observability(
        service="api",
        version="test",
        tracer_provider=provider,
        log_sink=broken_sink,
        clock_ns=StepClock(),
        wall_clock=lambda: NOW,
    )
    client = TestClient(
        _app(telemetry), headers=AUTH_HEADERS, raise_server_exceptions=False
    )

    response = client.get(
        "/v1/jobs/safe-job",
        headers=[
            ("Traceparent", TRACEPARENT),
            ("Traceparent", TRACEPARENT),
            ("X-Request-ID", "duplicate-trace-request"),
        ],
    )

    assert response.status_code == 200
    span = _finished(exporter)[0]
    assert span.context is not None
    assert f"{span.context.trace_id:032x}" != TRACE_ID
    assert span.parent is None
    provider.shutdown()


def test_api_telemetry_failures_never_interrupt_request_processing() -> None:
    class BrokenTelemetry:
        def __getattribute__(self, _name: str) -> Never:
            raise RuntimeError("private-telemetry-failure-sentinel")

    client = TestClient(
        _app(cast(Observability, BrokenTelemetry())),
        headers=AUTH_HEADERS,
        raise_server_exceptions=False,
    )

    response = client.get("/v1/jobs/safe-job")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "private-telemetry-failure-sentinel" not in response.text


def test_operational_metrics_refresh_from_one_fixed_aggregate_snapshot() -> None:
    class Snapshot:
        queued_jobs = 2
        failed_jobs = 3
        input_units = 5
        output_units = 7

    calls = 0

    def snapshot_provider() -> Snapshot:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("private-persistence-error-sentinel")
        return Snapshot()

    telemetry = Observability(
        service="api",
        operational_snapshot_provider=snapshot_provider,
    )

    first = telemetry.render_metrics().body.decode("utf-8")
    second = telemetry.render_metrics().body.decode("utf-8")

    assert "control_plane_job_queue_depth 2.0" in first
    assert "control_plane_failed_jobs 3.0" in first
    assert 'control_plane_evaluation_usage_units{direction="input"} 5.0' in first
    assert 'control_plane_evaluation_usage_units{direction="output"} 7.0' in first
    assert "control_plane_operational_snapshot_ready 1.0" in first
    assert "control_plane_operational_snapshot_ready 0.0" in second
    assert "control_plane_job_queue_depth 2.0" in second
    assert "private-persistence-error-sentinel" not in first + second
    telemetry.shutdown()


def test_configuration_and_clock_fail_closed_without_retaining_values() -> None:
    def broken_clock() -> int:
        raise RuntimeError("private-clock-value")

    telemetry = Observability(
        service="api",
        clock_ns=broken_clock,
        wall_clock=lambda: NOW,
    )
    telemetry.request_started()
    telemetry.request_finished(
        method="private-method",
        route="/private-route",
        status_code=999,
        error_code="private-error",
        duration_ns=-1,
    )
    metrics = telemetry.render_metrics().body.decode()

    assert 'method="OTHER"' in metrics
    assert 'route="unmatched"' in metrics
    assert 'status_class="5xx"' in metrics
    assert 'code="unknown"' in metrics
    for sentinel in (
        "private-clock-value",
        "private-method",
        "/private-route",
        "private-error",
    ):
        assert sentinel not in metrics
    telemetry.shutdown()
