from __future__ import annotations

from typing import Never, cast

from pytest import MonkeyPatch
from starlette.types import Scope

from llm_eval_control_plane.api.observability import (
    current_traceparent,
    route_template_from_scope,
    set_error_code,
    trace_context_from_scope,
)

TRACE_ID = "1234567890abcdef1234567890abcdef"
SPAN_ID = "1234567890abcdef"


class UnreadableRoute:
    @property
    def path(self) -> Never:
        raise RuntimeError("private-route-value")


def test_trace_context_rejects_non_ascii_and_zero_identifiers() -> None:
    non_ascii = cast(
        Scope,
        {"type": "http", "headers": [(b"traceparent", b"\xff")]},
    )
    zero_trace = cast(
        Scope,
        {
            "type": "http",
            "headers": [(b"traceparent", f"00-{'0' * 32}-{SPAN_ID}-01".encode())],
        },
    )
    zero_span = cast(
        Scope,
        {
            "type": "http",
            "headers": [(b"traceparent", f"00-{TRACE_ID}-{'0' * 16}-01".encode())],
        },
    )

    assert trace_context_from_scope(non_ascii) is None
    assert trace_context_from_scope(zero_trace) is None
    assert trace_context_from_scope(zero_span) is None


def test_scope_helpers_ignore_unreadable_or_wrongly_typed_state() -> None:
    scope = cast(
        Scope,
        {
            "type": "http",
            "route": UnreadableRoute(),
            "state": "private-state-value",
        },
    )

    assert route_template_from_scope(scope) == "unmatched"
    set_error_code(scope, "internal_error")
    assert scope["state"] == "private-state-value"


def test_current_trace_context_failures_are_isolated(monkeypatch: MonkeyPatch) -> None:
    assert current_traceparent() is None

    def broken_current_span() -> Never:
        raise RuntimeError("private-current-span-value")

    monkeypatch.setattr(
        "llm_eval_control_plane.api.observability.get_current_span",
        broken_current_span,
    )

    assert current_traceparent() is None
