from __future__ import annotations

import json
from datetime import datetime
from typing import Never, cast

from opentelemetry.trace import Status, StatusCode
from pytest import raises

from llm_eval_control_plane.observability import (
    Observability,
    safe_span_id,
    safe_trace_id,
)


class UnprintableValue:
    def __str__(self) -> Never:
        raise RuntimeError("private-string-value")


def test_service_specific_telemetry_rejects_cross_service_and_hostile_values() -> None:
    def broken_clock() -> int:
        raise RuntimeError("private-clock-value")

    with raises(ValueError, match="Observability configuration is invalid"):
        Observability(service="private-service")

    telemetry = Observability(service="api", clock_ns=broken_clock)

    assert "/v1/jobs/{job_id}" in telemetry.allowed_http_routes
    assert "/private-resource" not in telemetry.allowed_http_routes
    assert telemetry.now_ns() == 0
    assert safe_trace_id("0" * 32) == "0" * 32
    assert safe_trace_id("PRIVATE") == "0" * 32
    assert safe_span_id("0" * 16) == "0" * 16
    assert safe_span_id(object()) == "0" * 16

    telemetry.record_worker_poll("private-outcome")
    telemetry.record_worker_job_result("succeeded")
    telemetry.record_worker_recovery(3)
    telemetry.set_worker_ready(True)
    telemetry.emit_worker_lifecycle("started")
    with telemetry.trace_job(
        UnprintableValue(),
        cast(str | None, object()),
    ):
        pass

    rendered = telemetry.render_metrics().body.decode("utf-8")
    assert "control_plane_worker_" not in rendered
    assert "private" not in rendered
    telemetry.shutdown()


def test_invalid_operational_snapshot_fails_closed_without_partial_updates() -> None:
    class InvalidSnapshot:
        queued_jobs = -1
        failed_jobs = 2
        input_units = 3
        output_units = 5

    telemetry = Observability(
        service="api",
        operational_snapshot_provider=InvalidSnapshot,
    )

    rendered = telemetry.render_metrics().body.decode("utf-8")

    assert "control_plane_operational_snapshot_ready 0.0" in rendered
    assert "control_plane_job_queue_depth 0.0" in rendered
    assert "control_plane_failed_jobs 0.0" in rendered
    telemetry.shutdown()


def test_worker_telemetry_bounds_invalid_values_and_trace_links() -> None:
    lines: list[str] = []
    telemetry = Observability(
        service="worker",
        version="test",
        log_sink=lines.append,
    )
    zero_traceparent = f"00-{'0' * 32}-{'1' * 16}-01"

    telemetry.record_worker_job_result("private-result")
    telemetry.set_worker_ready(cast(bool, 1))
    telemetry.emit_worker_lifecycle("private-state")
    telemetry.record_worker_recovery(10_001)
    with telemetry.trace_job(
        UnprintableValue(),
        cast(str | None, object()),
    ):
        pass
    with telemetry.trace_job("run", zero_traceparent):
        pass
    telemetry.shutdown()

    documents = tuple(json.loads(line) for line in lines)
    job_events = tuple(
        document
        for document in documents
        if document["event"] == "worker.job.completed"
    )
    span_events = tuple(
        document
        for document in documents
        if document["event"] == "trace.span.completed"
        and document["operation"] == "worker.job"
    )
    recovery = next(
        document
        for document in documents
        if document["event"] == "worker.recovery.completed"
    )

    assert [event["job_kind"] for event in job_events] == ["unknown", "run"]
    assert recovery["recovered_jobs"] == 10_000
    assert len(span_events) == 2
    assert all("linked_trace_id" not in event for event in span_events)
    assert "private" not in "".join(lines)


def test_safe_span_exporter_bounds_status_time_and_sink_failures() -> None:
    lines: list[str] = []
    telemetry = Observability(
        service="api",
        version="test",
        log_sink=lines.append,
        wall_clock=lambda: datetime(2026, 8, 25, 12, 34, 56),
    )

    with telemetry.tracer.start_as_current_span(
        "private-operation",
        record_exception=False,
        set_status_on_exception=False,
    ) as error_span:
        error_span.set_status(Status(StatusCode.ERROR))
    with telemetry.tracer.start_as_current_span(
        "evaluation.run",
        record_exception=False,
        set_status_on_exception=False,
    ) as ok_span:
        ok_span.set_status(Status(StatusCode.OK))
    telemetry.shutdown()

    documents = tuple(json.loads(line) for line in lines)
    assert [(item["operation"], item["outcome"]) for item in documents] == [
        ("unknown", "error"),
        ("evaluation.run", "ok"),
    ]
    assert documents[0]["severity"] == "ERROR"
    assert all(item["timestamp"].endswith("Z") for item in documents)
    assert "private-operation" not in "".join(lines)

    def broken_sink(_document: str) -> Never:
        raise RuntimeError("private-log-sink-value")

    broken = Observability(service="api", log_sink=broken_sink)
    with broken.tracer.start_as_current_span(
        "evaluation.run",
        record_exception=False,
        set_status_on_exception=False,
    ):
        pass
    broken.shutdown()
