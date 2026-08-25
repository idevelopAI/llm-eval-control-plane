"""Dependency-injected, privacy-safe telemetry primitives.

The module deliberately avoids global metric and tracer providers.  Callers receive a
dedicated Prometheus registry and an isolated OpenTelemetry provider whose attributes
are supplied only by the bounded adapters in this package.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.trace import Tracer
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
        "/v1/runs",
        "/v1/runs/{run_id}",
    }
)
_ERROR_CODES = frozenset(
    {
        "control_plane_error",
        "idempotency_conflict",
        "internal_error",
        "invalid_cursor",
        "invalid_json",
        "invalid_request",
        "invalid_submission",
        "method_not_allowed",
        "persistence_unavailable",
        "request_body_too_large",
        "resource_conflict",
        "resource_not_found",
        "route_not_found",
        "unsupported_content_encoding",
        "unsupported_media_type",
    }
)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
_MAX_DURATION_NS = 86_400 * 1_000_000_000


class LogSink(Protocol):
    """A destination for one already-sanitized JSON line."""

    def __call__(self, document: str, /) -> object: ...


@dataclass(frozen=True, slots=True)
class MetricsDocument:
    """A rendered Prometheus document ready for an internal HTTP response."""

    body: bytes
    content_type: str


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
        "_owns_provider",
        "_provider",
        "_registry",
        "_service",
        "_tracer",
        "_wall_clock",
    )

    def __init__(
        self,
        *,
        service: str,
        version: str = "0.1.0",
        tracer_provider: ApiTracerProvider | None = None,
        registry: CollectorRegistry | None = None,
        log_sink: LogSink | None = None,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if service not in _SERVICE_NAMES or _VERSION.fullmatch(version) is None:
            raise ValueError("Observability configuration is invalid")
        self._service = service
        self._registry = registry or CollectorRegistry(auto_describe=True)
        self._log_sink = log_sink
        self._clock_ns = clock_ns
        self._wall_clock = wall_clock

        if tracer_provider is None:
            provider: ApiTracerProvider = TracerProvider(
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
        return MetricsDocument(
            body=generate_latest(self._registry),
            content_type=CONTENT_TYPE_LATEST,
        )

    def shutdown(self) -> None:
        """Shut down an internally owned provider without touching global state."""
        if self._owns_provider and isinstance(self._provider, TracerProvider):
            self._provider.shutdown()

    def _timestamp(self) -> str:
        try:
            value = self._wall_clock()
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError
            normalized = value.astimezone(UTC)
        except Exception:
            normalized = datetime.now(UTC)
        return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


__all__ = [
    "MetricsDocument",
    "Observability",
    "bounded_duration_ns",
    "safe_error_code",
    "safe_http_method",
    "safe_http_route",
    "safe_http_status",
    "safe_request_id",
    "safe_span_id",
    "safe_trace_id",
]
