"""Manual privacy-safe ASGI request telemetry."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import TypeVar

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
_Value = TypeVar("_Value")


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

        started_ns = _telemetry_value(lambda: self._telemetry.now_ns(), 0)
        method = safe_http_method(scope.get("method"))
        response_status: int | None = None
        _telemetry_action(lambda: self._telemetry.request_started())

        async def observe_send(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start" and response_status is None:
                response_status = safe_http_status(message.get("status"))
            await send(message)

        span: Span = NonRecordingSpan(INVALID_SPAN.get_span_context())
        span_context: AbstractContextManager[Span] | None = None
        try:
            parent = trace_context_from_scope(scope)
            span_context = self._telemetry.tracer.start_as_current_span(
                "HTTP request",
                context=parent if parent is not None else Context(),
                kind=SpanKind.SERVER,
                record_exception=False,
                set_status_on_exception=False,
            )
            span = span_context.__enter__()
        except Exception:
            if span_context is not None:
                failed_context = span_context
                _telemetry_action(lambda: failed_context.__exit__(None, None, None))
            span_context = None

        try:
            await self._app(scope, receive, observe_send)
        except BaseException:
            response_status = 500
            _set_error_code(scope, "internal_error")
            raise
        finally:
            ended_ns = _telemetry_value(lambda: self._telemetry.now_ns(), started_ns)
            duration_ns = max(0, ended_ns - started_ns)
            route = route_template_from_scope(scope)
            status_code = safe_http_status(response_status)
            error_code = error_code_from_scope(scope, status_code=status_code)
            request_id = request_id_from_scope(scope)
            _telemetry_action(lambda: span.update_name(f"{method} {route}"))
            _telemetry_action(lambda: span.set_attribute("http.request.method", method))
            _telemetry_action(lambda: span.set_attribute("http.route", route))
            _telemetry_action(
                lambda: span.set_attribute("http.response.status_code", status_code)
            )
            _telemetry_action(
                lambda: span.set_attribute("control_plane.request_id", request_id)
            )
            if error_code is not None:
                _telemetry_action(
                    lambda: span.set_attribute(
                        "control_plane.error_code",
                        error_code,
                    )
                )
            if status_code >= 500:
                _telemetry_action(lambda: span.set_status(Status(StatusCode.ERROR)))
            context = _telemetry_value(
                lambda: span.get_span_context(),
                INVALID_SPAN.get_span_context(),
            )
            trace_id = f"{context.trace_id:032x}"
            span_id = f"{context.span_id:016x}"
            _telemetry_action(
                lambda: self._telemetry.request_finished(
                    method=method,
                    route=route,
                    status_code=status_code,
                    error_code=error_code,
                    duration_ns=duration_ns,
                )
            )
            auth_outcome = _auth_outcome_from_scope(scope)
            if auth_outcome is not None:
                _telemetry_action(
                    lambda: self._telemetry.record_auth_decision(auth_outcome)
                )
            _telemetry_action(
                lambda: self._telemetry.emit_http_event(
                    request_id=request_id,
                    trace_id=trace_id,
                    span_id=span_id,
                    method=method,
                    route=route,
                    status_code=status_code,
                    error_code=error_code,
                    duration_ns=duration_ns,
                )
            )
            if span_context is not None:
                _telemetry_action(lambda: span_context.__exit__(None, None, None))


def _telemetry_value(action: Callable[[], _Value], fallback: _Value) -> _Value:
    try:
        return action()
    except Exception:
        return fallback


def _telemetry_action(action: Callable[[], object]) -> None:
    try:
        action()
    except Exception:
        return


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
