"""Manual privacy-safe ASGI request telemetry."""

from __future__ import annotations

import re
from contextlib import AbstractContextManager, nullcontext
from typing import cast

from opentelemetry.context import Context
from opentelemetry.trace import (
    INVALID_SPAN,
    NonRecordingSpan,
    Span,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
    TraceState,
    get_current_span,
    set_span_in_context,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from llm_eval_control_plane.observability import (
    Observability,
    safe_error_code,
    safe_http_method,
    safe_http_route,
    safe_http_status,
    safe_request_id,
)

_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-(00|01)$")


class ApiObservabilityMiddleware:
    """Record one bounded event, span, and metric observation per HTTP request."""

    __slots__ = ("_app", "_telemetry")

    def __init__(self, app: ASGIApp, *, telemetry: Observability) -> None:
        self._app = app
        self._telemetry = telemetry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        started_ns = self._telemetry.now_ns()
        method = safe_http_method(scope.get("method"))
        response_status: int | None = None
        self._telemetry.request_started()

        async def observe_send(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start" and response_status is None:
                response_status = safe_http_status(message.get("status"))
            await send(message)

        parent = trace_context_from_scope(scope)
        span_context: AbstractContextManager[Span]
        try:
            span_context = self._telemetry.tracer.start_as_current_span(
                "HTTP request",
                context=parent,
                kind=SpanKind.SERVER,
                record_exception=False,
                set_status_on_exception=False,
            )
        except Exception:
            span_context = cast(
                AbstractContextManager[Span],
                nullcontext(NonRecordingSpan(INVALID_SPAN.get_span_context())),
            )

        with span_context as span:
            try:
                await self._app(scope, receive, observe_send)
            except BaseException:
                response_status = 500
                _set_error_code(scope, "internal_error")
                raise
            finally:
                ended_ns = self._telemetry.now_ns()
                duration_ns = max(0, ended_ns - started_ns)
                route = route_template_from_scope(scope)
                status_code = safe_http_status(response_status)
                error_code = error_code_from_scope(scope, status_code=status_code)
                request_id = request_id_from_scope(scope)
                span.update_name(f"{method} {route}")
                span.set_attribute("http.request.method", method)
                span.set_attribute("http.route", route)
                span.set_attribute("http.response.status_code", status_code)
                span.set_attribute("control_plane.request_id", request_id)
                if error_code is not None:
                    span.set_attribute("control_plane.error_code", error_code)
                if status_code >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                context = span.get_span_context()
                trace_id = f"{context.trace_id:032x}"
                span_id = f"{context.span_id:016x}"
                self._telemetry.request_finished(
                    method=method,
                    route=route,
                    status_code=status_code,
                    error_code=error_code,
                    duration_ns=duration_ns,
                )
                auth_outcome = _auth_outcome_from_scope(scope)
                if auth_outcome is not None:
                    self._telemetry.record_auth_decision(auth_outcome)
                self._telemetry.emit_http_event(
                    request_id=request_id,
                    trace_id=trace_id,
                    span_id=span_id,
                    method=method,
                    route=route,
                    status_code=status_code,
                    error_code=error_code,
                    duration_ns=duration_ns,
                )


def trace_context_from_scope(scope: Scope) -> Context | None:
    """Accept only one strict lowercase W3C traceparent and no other context."""
    values = [
        value
        for name, value in scope.get("headers", [])
        if name.lower() == b"traceparent"
    ]
    if len(values) != 1:
        return None
    try:
        candidate = values[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    match = _TRACEPARENT.fullmatch(candidate)
    if match is None:
        return None
    trace_id_text, span_id_text, flags_text = match.groups()
    if trace_id_text == "0" * 32 or span_id_text == "0" * 16:
        return None
    parent = SpanContext(
        trace_id=int(trace_id_text, 16),
        span_id=int(span_id_text, 16),
        is_remote=True,
        trace_flags=TraceFlags(int(flags_text, 16)),
        trace_state=TraceState(),
    )
    return set_span_in_context(NonRecordingSpan(parent))


def current_traceparent() -> str | None:
    """Return the active span as strict W3C coordination metadata."""
    try:
        context = get_current_span().get_span_context()
    except Exception:
        return None
    if not context.is_valid:
        return None
    flags = "01" if context.trace_flags.sampled else "00"
    return f"00-{context.trace_id:032x}-{context.span_id:016x}-{flags}"


def route_template_from_scope(scope: Scope) -> str:
    """Return a fixed route template, never the path or raw request target."""
    route = scope.get("route")
    try:
        candidate = getattr(route, "path", None)
    except Exception:
        return "unmatched"
    return safe_http_route(candidate)


def request_id_from_scope(scope: Scope) -> str:
    state = scope.get("state")
    value = state.get("request_id") if isinstance(state, dict) else None
    return safe_request_id(value)


def error_code_from_scope(scope: Scope, *, status_code: int) -> str | None:
    if status_code < 400:
        return None
    state = scope.get("state")
    value = state.get("observability_error_code") if isinstance(state, dict) else None
    return safe_error_code(value)


def set_error_code(scope: Scope, code: str) -> None:
    """Attach one safe public error code for the outer telemetry middleware."""
    _set_error_code(scope, safe_error_code(code))


def _set_error_code(scope: Scope, code: str) -> None:
    state = scope.setdefault("state", {})
    if isinstance(state, dict):
        state["observability_error_code"] = code


def _auth_outcome_from_scope(scope: Scope) -> str | None:
    state = scope.get("state")
    value = state.get("auth_outcome") if isinstance(state, dict) else None
    return value if value in {"allowed", "denied", "invalid"} else None


__all__ = [
    "ApiObservabilityMiddleware",
    "current_traceparent",
    "error_code_from_scope",
    "request_id_from_scope",
    "route_template_from_scope",
    "set_error_code",
    "trace_context_from_scope",
]
