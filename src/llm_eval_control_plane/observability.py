"""Dependency-injected, privacy-safe telemetry primitives.

The module deliberately avoids global metric and tracer providers.  Callers receive a
dedicated Prometheus registry and an isolated OpenTelemetry provider whose attributes
are supplied only by the bounded adapters in this package.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import (
    INVALID_SPAN,
    Link,
    NonRecordingSpan,
    Span,
    SpanContext,
    SpanKind,
    StatusCode,
    TraceFlags,
    Tracer,
    TraceState,
)
from opentelemetry.trace import TracerProvider as ApiTracerProvider
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import CONTENT_TYPE_LATEST, generate_latest

_SERVICE_NAMES = frozenset({"api", "worker"})
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_AUTH_OUTCOMES = frozenset({"allowed", "denied", "invalid", "missing", "unavailable"})
_HTTP_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
_HTTP_ROUTES = frozenset(
    {
        "/docs",
        "/docs/oauth2-redirect",
        "/health/live",
        "/health/ready",
        "/metrics",
        "/openapi.json",
        "/v1/comparisons",
        "/v1/dataset-revisions/{revision}/{name:path}",
        "/v1/datasets",
        "/v1/jobs",
        "/v1/jobs/{job_id}",
        "/v1/jobs/{job_id}/attempts",
        "/v1/jobs/{job_id}/cancellation",
        "/v1/release-decisions",
        "/v1/release-decisions/{decision_id}",
        "/v1/release-decisions/{decision_id}/cases",
        "/v1/release-decisions/{decision_id}/distributions",
        "/v1/runs",
        "/v1/runs/{run_id}",
    }
)
_ERROR_CODES = frozenset(
    {
        "control_plane_error",
        "authentication_required",
        "idempotency_conflict",
        "internal_error",
        "invalid_cursor",
        "invalid_json",
        "invalid_request",
        "invalid_submission",
        "method_not_allowed",
        "persistence_unavailable",
        "permission_denied",
        "readiness_unavailable",
        "request_body_too_large",
        "resource_conflict",
        "resource_not_found",
        "route_not_found",
        "unsupported_content_encoding",
        "unsupported_media_type",
    }
)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_WORKER_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_WORKER_JOB_KINDS = frozenset({"comparison", "run"})
_WORKER_JOB_RESULTS = frozenset(
    {"canceled", "failed", "lease_lost", "retry_scheduled", "succeeded"}
)
_WORKER_POLL_OUTCOMES = frozenset({"idle", "processed", "unavailable"})
_WORKER_LIFECYCLE_STATES = frozenset(
    {
        "not_ready",
        "persistence_recovered",
        "persistence_unavailable",
        "ready",
        "started",
        "stopped",
    }
)
_TRACE_OPERATIONS = frozenset(
    {
        "evaluation.evaluator.evaluate",
        "evaluation.run",
        "evaluation.target.invoke",
        "worker.job",
        *(
            f"{method} {route}"
            for method in _HTTP_METHODS
            for route in (*_HTTP_ROUTES, "unmatched")
        ),
    }
)
_TRACE_KINDS = {
    SpanKind.CONSUMER: "consumer",
    SpanKind.INTERNAL: "internal",
    SpanKind.SERVER: "server",
}
_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
_MAX_DURATION_NS = 86_400 * 1_000_000_000
_MAX_RECOVERED_JOBS = 10_000
_MAX_OPERATIONAL_VALUE = 2**63 - 1


class LogSink(Protocol):
    """A destination for one already-sanitized JSON line."""

    def __call__(self, document: str, /) -> object: ...


class OperationalSnapshot(Protocol):
    """Fixed aggregate values supplied by the persistence adapter."""

    @property
    def queued_jobs(self) -> int: ...

    @property
    def failed_jobs(self) -> int: ...

    @property
    def input_units(self) -> int: ...

    @property
    def output_units(self) -> int: ...


@dataclass(frozen=True, slots=True)
class MetricsDocument:
    """A rendered Prometheus document ready for an internal HTTP response."""

    body: bytes
    content_type: str


class _SafeJsonSpanExporter(SpanExporter):
    """Export only a fixed span envelope; attributes and events stay private."""

    __slots__ = ("_log_sink", "_service", "_wall_clock")

    def __init__(
        self,
        *,
        service: str,
        log_sink: LogSink,
        wall_clock: Callable[[], datetime],
    ) -> None:
        self._service = service
        self._log_sink = log_sink
        self._wall_clock = wall_clock

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for span in spans:
            self._export_one(span)
        return SpanExportResult.SUCCESS

    def _export_one(self, span: ReadableSpan) -> None:
        try:
            context = span.context
            if context is None:
                return
            status = span.status.status_code
            event: dict[str, object] = {
                "duration_ms": bounded_duration_ns(
                    _span_duration_ns(span.start_time, span.end_time)
                )
                // 1_000_000,
                "event": "trace.span.completed",
                "operation": _safe_trace_operation(span.name),
                "outcome": _safe_trace_outcome(status),
                "schema_version": "control-plane-log/v1",
                "service": self._service,
                "severity": "ERROR" if status is StatusCode.ERROR else "INFO",
                "span_id": safe_span_id(f"{context.span_id:016x}"),
                "span_kind": _TRACE_KINDS.get(span.kind, "unknown"),
                "timestamp": _safe_timestamp(self._wall_clock),
                "trace_id": safe_trace_id(f"{context.trace_id:032x}"),
            }
            parent = span.parent
            if parent is not None and parent.is_valid:
                event["parent_span_id"] = safe_span_id(f"{parent.span_id:016x}")
            links = tuple(link for link in span.links if link.context.is_valid)
            if links:
                linked = links[0].context
                event["linked_span_id"] = safe_span_id(f"{linked.span_id:016x}")
                event["linked_trace_id"] = safe_trace_id(f"{linked.trace_id:032x}")
            document = json.dumps(
                event,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            self._log_sink(f"{document}\n")
        except Exception:
            return


class Observability:
    """Own isolated metrics, tracing, and fixed-schema structured events."""

    __slots__ = (
        "_auth_decisions",
        "_clock_ns",
        "_http_duration",
        "_http_errors",
        "_http_in_progress",
        "_http_requests",
        "_log_sink",
        "_operational_failed_jobs",
        "_operational_queue_depth",
        "_operational_snapshot_provider",
        "_operational_snapshot_ready",
        "_operational_usage",
        "_owns_provider",
        "_provider",
        "_registry",
        "_service",
        "_tracer",
        "_wall_clock",
        "_worker_job_duration",
        "_worker_job_results",
        "_worker_polls",
        "_worker_ready",
        "_worker_reaper_recovered_jobs",
        "_worker_reaper_runs",
    )

    def __init__(
        self,
        *,
        service: str,
        version: str = "0.1.0",
        tracer_provider: ApiTracerProvider | None = None,
        registry: CollectorRegistry | None = None,
        log_sink: LogSink | None = None,
        operational_snapshot_provider: Callable[[], OperationalSnapshot] | None = None,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if service not in _SERVICE_NAMES or _VERSION.fullmatch(version) is None:
            raise ValueError("Observability configuration is invalid")
        self._service = service
        self._registry = registry or CollectorRegistry(auto_describe=True)
        self._log_sink = log_sink
        self._operational_snapshot_provider = operational_snapshot_provider
        self._clock_ns = clock_ns
        self._wall_clock = wall_clock

        if tracer_provider is None:
            internal_provider = TracerProvider(
                resource=Resource(
                    {
                        "service.name": f"llm-eval-control-plane-{service}",
                        "service.version": version,
                    }
                ),
                span_limits=SpanLimits(
                    max_attributes=16,
                    max_events=0,
                    max_links=1,
                    max_attribute_length=128,
                ),
            )
            if log_sink is not None:
                internal_provider.add_span_processor(
                    SimpleSpanProcessor(
                        _SafeJsonSpanExporter(
                            service=service,
                            log_sink=log_sink,
                            wall_clock=wall_clock,
                        )
                    )
                )
            provider: ApiTracerProvider = internal_provider
            self._owns_provider = True
        else:
            provider = tracer_provider
            self._owns_provider = False
        self._provider = provider
        self._tracer = provider.get_tracer(
            "llm_eval_control_plane.observability",
            version,
        )

        self._http_requests = Counter(
            "control_plane_http_requests_total",
            "Completed control-plane HTTP requests.",
            ("method", "route", "status_class"),
            registry=self._registry,
        )
        self._http_duration = Histogram(
            "control_plane_http_request_duration_seconds",
            "Control-plane HTTP request duration in seconds.",
            ("method", "route"),
            buckets=_DURATION_BUCKETS,
            registry=self._registry,
        )
        self._http_errors = Counter(
            "control_plane_http_errors_total",
            "Completed control-plane HTTP errors by stable code.",
            ("route", "code"),
            registry=self._registry,
        )
        self._http_in_progress = Gauge(
            "control_plane_http_requests_in_progress",
            "Control-plane HTTP requests currently in progress.",
            registry=self._registry,
        )
        self._auth_decisions = Counter(
            "control_plane_auth_decisions_total",
            "Control-plane authentication decisions.",
            ("outcome",),
            registry=self._registry,
        )
        self._operational_queue_depth: Gauge | None = None
        self._operational_failed_jobs: Gauge | None = None
        self._operational_usage: Gauge | None = None
        self._operational_snapshot_ready: Gauge | None = None
        if service == "api":
            self._operational_queue_depth = Gauge(
                "control_plane_job_queue_depth",
                "Persisted jobs currently waiting to be claimed.",
                registry=self._registry,
            )
            self._operational_failed_jobs = Gauge(
                "control_plane_failed_jobs",
                "Persisted jobs in the terminal failed state.",
                registry=self._registry,
            )
            self._operational_usage = Gauge(
                "control_plane_evaluation_usage_units",
                "Aggregate persisted evaluation usage units.",
                ("direction",),
                registry=self._registry,
            )
            self._operational_snapshot_ready = Gauge(
                "control_plane_operational_snapshot_ready",
                "Whether the latest persisted operational snapshot succeeded.",
                registry=self._registry,
            )
        self._worker_polls: Counter | None = None
        self._worker_job_duration: Histogram | None = None
        self._worker_job_results: Counter | None = None
        self._worker_reaper_runs: Counter | None = None
        self._worker_reaper_recovered_jobs: Counter | None = None
        self._worker_ready: Gauge | None = None
        if service == "worker":
            self._worker_polls = Counter(
                "control_plane_worker_polls_total",
                "Completed worker polling cycles.",
                ("outcome",),
                registry=self._registry,
            )
            self._worker_job_duration = Histogram(
                "control_plane_worker_job_duration_seconds",
                "Claimed worker job processing duration in seconds.",
                ("kind", "outcome"),
                buckets=_DURATION_BUCKETS,
                registry=self._registry,
            )
            self._worker_job_results = Counter(
                "control_plane_worker_job_results_total",
                "Durable worker job results.",
                ("result",),
                registry=self._registry,
            )
            self._worker_reaper_runs = Counter(
                "control_plane_worker_reaper_runs_total",
                "Completed expired-job recovery sweeps.",
                ("outcome",),
                registry=self._registry,
            )
            self._worker_reaper_recovered_jobs = Counter(
                "control_plane_worker_reaper_recovered_jobs_total",
                "Jobs recovered from expired worker leases.",
                registry=self._registry,
            )
            self._worker_ready = Gauge(
                "control_plane_worker_ready",
                "Whether the worker is ready to claim durable jobs.",
                registry=self._registry,
            )

    @property
    def tracer(self) -> Tracer:
        """Return the isolated manual tracer used by transport adapters."""
        return self._tracer

    @property
    def allowed_http_routes(self) -> frozenset[str]:
        """Return the exact route-template vocabulary accepted by telemetry."""
        return _HTTP_ROUTES

    def now_ns(self) -> int:
        """Read the monotonic clock without exposing clock errors to requests."""
        try:
            value = self._clock_ns()
        except Exception:
            return 0
        return value if type(value) is int and value >= 0 else 0

    def request_started(self) -> None:
        self._http_in_progress.inc()

    def request_finished(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        error_code: str | None,
        duration_ns: int,
    ) -> None:
        """Record one request using bounded labels only."""
        safe_method = safe_http_method(method)
        safe_route = safe_http_route(route)
        safe_status = safe_http_status(status_code)
        safe_duration_ns = bounded_duration_ns(duration_ns)
        self._http_in_progress.dec()
        self._http_requests.labels(
            method=safe_method,
            route=safe_route,
            status_class=f"{safe_status // 100}xx",
        ).inc()
        self._http_duration.labels(
            method=safe_method,
            route=safe_route,
        ).observe(safe_duration_ns / 1_000_000_000)
        if safe_status >= 400:
            self._http_errors.labels(
                route=safe_route,
                code=safe_error_code(error_code),
            ).inc()

    def record_auth_decision(self, outcome: str) -> None:
        """Count a fixed authentication outcome without subject identifiers."""
        safe_outcome = outcome if outcome in _AUTH_OUTCOMES else "other"
        self._auth_decisions.labels(outcome=safe_outcome).inc()

    def record_worker_poll(self, outcome: str) -> None:
        """Count one poll with a fixed outcome and no work identifiers."""
        if self._worker_polls is None:
            return
        safe_outcome = outcome if outcome in _WORKER_POLL_OUTCOMES else "unavailable"
        try:
            self._worker_polls.labels(outcome=safe_outcome).inc()
        except Exception:
            return

    def record_worker_job_result(self, result: str) -> None:
        """Count one durable result without retaining the claimed job identity."""
        if self._worker_job_results is None or result not in _WORKER_JOB_RESULTS:
            return
        try:
            self._worker_job_results.labels(result=result).inc()
        except Exception:
            return

    def record_worker_recovery(self, recovered_jobs: int) -> None:
        """Record one bounded recovery sweep and emit only material recovery."""
        if self._worker_reaper_runs is None:
            return
        safe_count = (
            min(recovered_jobs, _MAX_RECOVERED_JOBS)
            if type(recovered_jobs) is int and recovered_jobs >= 0
            else 0
        )
        with suppress(Exception):
            outcome = "recovered" if safe_count else "none"
            self._worker_reaper_runs.labels(outcome=outcome).inc()
            if safe_count and self._worker_reaper_recovered_jobs is not None:
                self._worker_reaper_recovered_jobs.inc(safe_count)
        if safe_count:
            self._emit_worker_event(
                {
                    "event": "worker.recovery.completed",
                    "recovered_jobs": safe_count,
                    "severity": "WARNING",
                }
            )

    def set_worker_ready(self, ready: bool) -> None:
        """Publish one readiness transition selected by the worker runtime."""
        if self._worker_ready is None or type(ready) is not bool:
            return
        with suppress(Exception):
            self._worker_ready.set(1 if ready else 0)
        self.emit_worker_lifecycle("ready" if ready else "not_ready")

    def emit_worker_lifecycle(self, state: str) -> None:
        """Emit a lifecycle transition from a closed state vocabulary."""
        if state not in _WORKER_LIFECYCLE_STATES:
            return
        severity = (
            "WARNING" if state in {"not_ready", "persistence_unavailable"} else "INFO"
        )
        self._emit_worker_event(
            {
                "event": "worker.lifecycle",
                "severity": severity,
                "state": state,
            }
        )

    @contextmanager
    def trace_job(self, kind: object, traceparent: str | None) -> Iterator[None]:
        """Trace claimed work with a content-free span and at most one W3C link."""
        safe_kind = _safe_worker_job_kind(kind)
        started_ns = self.now_ns()
        span: Span = NonRecordingSpan(INVALID_SPAN.get_span_context())
        span_manager: AbstractContextManager[Span] | None = None
        link = _worker_link(traceparent)
        try:
            span_manager = self._tracer.start_as_current_span(
                "worker.job",
                context=Context(),
                kind=SpanKind.CONSUMER,
                links=(() if link is None else (link,)),
                record_exception=False,
                set_status_on_exception=False,
            )
            span = span_manager.__enter__()
        except Exception:
            span_manager = None

        outcome = "completed"
        try:
            yield
        except BaseException:
            outcome = "interrupted"
            raise
        finally:
            try:
                context = span.get_span_context()
            except Exception:
                context = INVALID_SPAN.get_span_context()
            trace_id = f"{context.trace_id:032x}"
            span_id = f"{context.span_id:016x}"
            if span_manager is not None:
                with suppress(Exception):
                    span_manager.__exit__(None, None, None)
            duration_ns = bounded_duration_ns(self.now_ns() - started_ns)
            if self._worker_job_duration is not None:
                with suppress(Exception):
                    self._worker_job_duration.labels(
                        kind=safe_kind,
                        outcome=outcome,
                    ).observe(duration_ns / 1_000_000_000)
            self._emit_worker_event(
                {
                    "duration_ms": duration_ns // 1_000_000,
                    "event": "worker.job.completed",
                    "job_kind": safe_kind,
                    "outcome": outcome,
                    "severity": "INFO" if outcome == "completed" else "WARNING",
                    "span_id": safe_span_id(span_id),
                    "trace_id": safe_trace_id(trace_id),
                }
            )

    def emit_http_event(
        self,
        *,
        request_id: str,
        trace_id: str,
        span_id: str,
        method: str,
        route: str,
        status_code: int,
        error_code: str | None,
        duration_ns: int,
    ) -> None:
        """Emit one fixed-schema request completion line."""
        if self._log_sink is None:
            return
        safe_status = safe_http_status(status_code)
        safe_error = safe_error_code(error_code) if safe_status >= 400 else None
        if safe_status >= 500:
            severity = "ERROR"
            outcome = "server_error"
        elif safe_status >= 400:
            severity = "WARNING"
            outcome = "client_error"
        else:
            severity = "INFO"
            outcome = "success"
        event: dict[str, object] = {
            "duration_ms": bounded_duration_ns(duration_ns) // 1_000_000,
            "event": "http.request.completed",
            "http_method": safe_http_method(method),
            "http_route": safe_http_route(route),
            "http_status": safe_status,
            "outcome": outcome,
            "request_id": safe_request_id(request_id),
            "schema_version": "control-plane-log/v1",
            "service": self._service,
            "severity": severity,
            "span_id": safe_span_id(span_id),
            "timestamp": self._timestamp(),
            "trace_id": safe_trace_id(trace_id),
        }
        if safe_error is not None:
            event["error_code"] = safe_error
        try:
            document = json.dumps(
                event,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            self._log_sink(f"{document}\n")
        except Exception:
            return

    def render_metrics(self) -> MetricsDocument:
        """Render only this instance's explicitly registered metrics."""
        self._refresh_operational_snapshot()
        return MetricsDocument(
            body=generate_latest(self._registry),
            content_type=CONTENT_TYPE_LATEST,
        )

    def _refresh_operational_snapshot(self) -> None:
        ready = self._operational_snapshot_ready
        provider = self._operational_snapshot_provider
        if ready is None:
            return
        if provider is None:
            with suppress(Exception):
                ready.set(0)
            return
        try:
            snapshot = provider()
            queue_depth = _bounded_operational_value(snapshot.queued_jobs)
            failed_jobs = _bounded_operational_value(snapshot.failed_jobs)
            input_units = _bounded_operational_value(snapshot.input_units)
            output_units = _bounded_operational_value(snapshot.output_units)
            if self._operational_queue_depth is not None:
                self._operational_queue_depth.set(queue_depth)
            if self._operational_failed_jobs is not None:
                self._operational_failed_jobs.set(failed_jobs)
            if self._operational_usage is not None:
                self._operational_usage.labels(direction="input").set(input_units)
                self._operational_usage.labels(direction="output").set(output_units)
            ready.set(1)
        except Exception:
            with suppress(Exception):
                ready.set(0)

    def shutdown(self) -> None:
        """Shut down an internally owned provider without touching global state."""
        if self._owns_provider and isinstance(self._provider, TracerProvider):
            self._provider.shutdown()

    def _emit_worker_event(self, fields: dict[str, object]) -> None:
        if self._service != "worker" or self._log_sink is None:
            return
        event = {
            **fields,
            "schema_version": "control-plane-log/v1",
            "service": "worker",
            "timestamp": self._timestamp(),
        }
        try:
            document = json.dumps(
                event,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            self._log_sink(f"{document}\n")
        except Exception:
            return

    def _timestamp(self) -> str:
        return _safe_timestamp(self._wall_clock)


def safe_http_method(value: object) -> str:
    return value if isinstance(value, str) and value in _HTTP_METHODS else "OTHER"


def safe_http_route(value: object) -> str:
    return value if isinstance(value, str) and value in _HTTP_ROUTES else "unmatched"


def safe_http_status(value: object) -> int:
    return value if type(value) is int and 100 <= value <= 599 else 500


def safe_error_code(value: object) -> str:
    return value if isinstance(value, str) and value in _ERROR_CODES else "unknown"


def safe_request_id(value: object) -> str:
    return (
        value
        if isinstance(value, str) and _REQUEST_ID.fullmatch(value) is not None
        else "request_unknown"
    )


def safe_trace_id(value: object) -> str:
    if (
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{32}", value) is not None
        and value != "0" * 32
    ):
        return value
    return "0" * 32


def safe_span_id(value: object) -> str:
    if (
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{16}", value) is not None
        and value != "0" * 16
    ):
        return value
    return "0" * 16


def bounded_duration_ns(value: object) -> int:
    if type(value) is not int or value < 0:
        return 0
    return min(value, _MAX_DURATION_NS)


def _bounded_operational_value(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_OPERATIONAL_VALUE:
        raise ValueError("Operational snapshot is invalid")
    return value


def _safe_trace_operation(value: object) -> str:
    return value if isinstance(value, str) and value in _TRACE_OPERATIONS else "unknown"


def _safe_trace_outcome(value: object) -> str:
    if value is StatusCode.ERROR:
        return "error"
    if value is StatusCode.OK:
        return "ok"
    return "unset"


def _span_duration_ns(start_time: object, end_time: object) -> int:
    if type(start_time) is not int or type(end_time) is not int:
        return 0
    return max(0, end_time - start_time)


def _safe_timestamp(wall_clock: Callable[[], datetime]) -> str:
    try:
        value = wall_clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        normalized = value.astimezone(UTC)
    except Exception:
        normalized = datetime.now(UTC)
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_worker_job_kind(value: object) -> str:
    try:
        candidate = str(value)
    except Exception:
        return "unknown"
    return candidate if candidate in _WORKER_JOB_KINDS else "unknown"


def _worker_link(value: object) -> Link | None:
    if not isinstance(value, str):
        return None
    match = _WORKER_TRACEPARENT.fullmatch(value)
    if match is None:
        return None
    trace_id_text, span_id_text, flags_text = match.groups()
    if trace_id_text == "0" * 32 or span_id_text == "0" * 16:
        return None
    try:
        context = SpanContext(
            trace_id=int(trace_id_text, 16),
            span_id=int(span_id_text, 16),
            is_remote=True,
            trace_flags=TraceFlags(int(flags_text, 16)),
            trace_state=TraceState(),
        )
        return Link(context)
    except Exception:
        return None


__all__ = [
    "MetricsDocument",
    "Observability",
    "OperationalSnapshot",
    "bounded_duration_ns",
    "safe_error_code",
    "safe_http_method",
    "safe_http_route",
    "safe_http_status",
    "safe_request_id",
    "safe_span_id",
    "safe_trace_id",
]
