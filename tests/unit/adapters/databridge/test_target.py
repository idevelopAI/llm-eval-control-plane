from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import NoReturn

import httpx
from pytest import MonkeyPatch, mark, raises

from llm_eval_control_plane.adapters.databridge import (
    DataBridgeHttpTarget,
    DataBridgeMockTarget,
)
from llm_eval_control_plane.application import TargetInvocationError
from llm_eval_control_plane.domain import (
    ArtifactKind,
    CanonicalJson,
    FailureCode,
    TargetOutcome,
    TargetRequest,
    TargetResponse,
)

_SECRET = "api-private-sentinel-should-never-be-persisted"


def query_request(
    case_id: str = "case-001",
    *,
    question: str = "How many users?",
    chat_history: str = "",
    language: str = "en",
    **extra: object,
) -> TargetRequest:
    return TargetRequest(
        case_id=case_id,
        input=CanonicalJson.from_value(
            {
                "question": question,
                "chat_history": chat_history,
                "language": language,
                **extra,
            }
        ),
    )


def success_body(
    *,
    status: str = "answered",
    executions: list[dict[str, object]] | None = None,
    input_tokens: int = 17,
    output_tokens: int = 9,
) -> dict[str, object]:
    if executions is None:
        executions = [
            {
                "sql": "SELECT count(*) FROM users",
                "columns": ["count"],
                "rows": [[3]],
                "row_count": 1,
                "truncated": False,
                "duration_ms": 1.25,
            }
        ]
    return {
        "status": status,
        "answer": "There are three users.",
        "executions": executions,
        "duration_ms": 4.5,
        "request_id": "request-private-provider-id",
        "model_duration_ms": 2.25,
        "tool_call_count": 1,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def http_target(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    timeout_seconds: float = 15.0,
    max_response_bytes: int = 256 * 1_024,
) -> DataBridgeHttpTarget:
    return DataBridgeHttpTarget(
        base_url="https://databridge.example",
        api_key_env="DATABRIDGE_API_KEY",
        synthetic_database_confirmed=True,
        transport=httpx.MockTransport(handler),
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
    )


def invoke(
    target: DataBridgeHttpTarget | DataBridgeMockTarget,
    request: TargetRequest | None = None,
) -> object:
    return asyncio.run(target.invoke(request or query_request()))


def json_response(
    body: object,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    encoded = json.dumps(body, separators=(",", ":")).encode()
    return httpx.Response(
        status,
        content=encoded,
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def test_http_target_sends_exact_expected_free_request_and_normalizes_response(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return json_response(success_body())

    monkeypatch.setenv("DATABRIDGE_API_KEY", _SECRET)
    target = http_target(handler)

    result = invoke(target)

    assert isinstance(result, TargetResponse)
    assert len(captured) == 1
    outbound = captured[0]
    assert outbound.method == "POST"
    assert str(outbound.url) == "https://databridge.example/api/v1/query"
    assert outbound.content == (
        b'{"chat_history":"","language":"en","question":"How many users?"}'
    )
    assert outbound.headers["content-type"] == "application/json"
    assert outbound.headers["x-api-key"] == _SECRET
    assert result.outcome is TargetOutcome.COMPLETED
    assert result.refusal_code is None
    assert result.usage.model_dump() == {"input_units": 17, "output_units": 9}
    assert result.output.to_value() == {
        "kind": "query",
        "schema_version": "1",
        "sql_executions": ["SELECT count(*) FROM users"],
    }
    persisted = result.model_dump_json()
    for stripped in (
        _SECRET,
        "There are three users.",
        "request-private-provider-id",
        '"rows"',
        '"columns"',
        "duration_ms",
    ):
        assert stripped not in persisted


def test_http_target_normalizes_clarification_without_provider_text(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABRIDGE_API_KEY", _SECRET)
    target = http_target(
        lambda _: json_response(
            success_body(status="clarification_required", executions=[])
        )
    )

    result = invoke(target)

    assert isinstance(result, TargetResponse)
    assert result.output.to_value() == {
        "clarification_code": "provider_clarification",
        "kind": "clarification",
        "schema_version": "1",
    }
    assert "There are three users" not in result.model_dump_json()


class ExplodingResponseStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise AssertionError("response body must not be read")
        yield b""  # pragma: no cover

    async def aclose(self) -> None:
        return None


class ChunkedResponseStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def test_http_403_becomes_body_independent_structured_refusal(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABRIDGE_API_KEY", _SECRET)
    target = http_target(
        lambda _: httpx.Response(403, stream=ExplodingResponseStream())
    )

    result = invoke(target)

    assert isinstance(result, TargetResponse)
    assert result.outcome is TargetOutcome.REFUSED
    assert result.refusal_code == "policy_block"
    assert result.usage.total_units == 0
    assert result.output.to_value() == {"kind": "refusal", "schema_version": "1"}


@mark.parametrize(
    ("status", "code", "retryable"),
    [
        (100, FailureCode.TARGET_PROTOCOL_ERROR, False),
        (201, FailureCode.TARGET_PROTOCOL_ERROR, False),
        (301, FailureCode.TARGET_PROTOCOL_ERROR, False),
        (307, FailureCode.TARGET_PROTOCOL_ERROR, False),
        (400, FailureCode.TARGET_REJECTED, False),
        (401, FailureCode.TARGET_AUTHENTICATION, False),
        (404, FailureCode.TARGET_REJECTED, False),
        (408, FailureCode.TARGET_TIMEOUT, True),
        (422, FailureCode.TARGET_REJECTED, False),
        (429, FailureCode.TARGET_RATE_LIMITED, True),
        (500, FailureCode.TARGET_UNAVAILABLE, True),
        (503, FailureCode.TARGET_UNAVAILABLE, True),
        (504, FailureCode.TARGET_TIMEOUT, True),
        (599, FailureCode.TARGET_UNAVAILABLE, True),
    ],
)
def test_http_target_maps_status_without_reading_error_bodies(
    monkeypatch: MonkeyPatch,
    status: int,
    code: FailureCode,
    retryable: bool,
) -> None:
    monkeypatch.setenv("DATABRIDGE_API_KEY", _SECRET)
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, stream=ExplodingResponseStream())

    with raises(TargetInvocationError) as captured:
        invoke(http_target(handler))

    assert calls == 1
    assert captured.value.code is code
    assert captured.value.retryable is retryable
    assert _SECRET not in str(captured.value)
    assert _SECRET not in repr(captured.value)


@mark.parametrize(
    ("body", "headers"),
    [
        (b"{}", {}),
        (b"not-json", {"Content-Type": "application/json"}),
        (
            b'{"status":"answered","status":"clarification_required"}',
            {"Content-Type": "application/json"},
        ),
        (b'{"duration_ms":NaN}', {"Content-Type": "application/json"}),
        (b'\xff{"status":"answered"}', {"Content-Type": "application/json"}),
        (
            json.dumps({**success_body(), "unexpected": True}).encode(),
            {"Content-Type": "application/json"},
        ),
        (
            json.dumps(success_body(input_tokens=-1)).encode(),
            {"Content-Type": "application/json"},
        ),
        (
            json.dumps(success_body(executions=[])).encode(),
            {"Content-Type": "application/json"},
        ),
        (
            json.dumps(success_body(status="clarification_required")).encode(),
            {"Content-Type": "application/json"},
        ),
    ],
)
def test_http_target_rejects_malformed_success_contracts(
    monkeypatch: MonkeyPatch,
    body: bytes,
    headers: dict[str, str],
) -> None:
    monkeypatch.setenv("DATABRIDGE_API_KEY", _SECRET)
    target = http_target(lambda _: httpx.Response(200, content=body, headers=headers))

    with raises(TargetInvocationError) as captured:
        invoke(target)

    assert captured.value.code is FailureCode.TARGET_PROTOCOL_ERROR
    assert captured.value.retryable is False


def test_http_target_accepts_json_charset_and_rejects_bad_content_length(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABRIDGE_API_KEY", _SECRET)
    accepted = http_target(
        lambda _: json_response(
            success_body(), headers={"Content-Type": "APPLICATION/JSON; charset=utf-8"}
        )
    )
    assert isinstance(invoke(accepted), TargetResponse)

    for content_length in ("invalid", "-1", "17"):

        def declared_response(
            _: httpx.Request,
            value: str = content_length,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": value,
                },
            )

        target = http_target(
            declared_response,
            max_response_bytes=16,
        )
        with raises(TargetInvocationError) as captured:
            invoke(target)
        assert captured.value.code is FailureCode.TARGET_PROTOCOL_ERROR


def test_http_target_enforces_decompressed_response_limit(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABRIDGE_API_KEY", _SECRET)
    target = http_target(
        lambda _: httpx.Response(
            200,
            stream=ChunkedResponseStream(b"x" * 16, b"x" * 17),
            headers={"Content-Type": "application/json"},
        ),
        max_response_bytes=32,
    )

    with raises(TargetInvocationError) as captured:
        invoke(target)

    assert captured.value.code is FailureCode.TARGET_PROTOCOL_ERROR


@mark.parametrize(
    ("exception", "code"),
    [
        (httpx.ReadTimeout("private transport detail"), FailureCode.TARGET_TIMEOUT),
        (
            httpx.ConnectError("private transport detail"),
            FailureCode.TARGET_UNAVAILABLE,
        ),
    ],
)
def test_http_target_sanitizes_transport_failures(
    monkeypatch: MonkeyPatch,
    exception: httpx.TransportError,
    code: FailureCode,
) -> None:
    monkeypatch.setenv("DATABRIDGE_API_KEY", _SECRET)

    def handler(_: httpx.Request) -> NoReturn:
        raise exception

    with raises(TargetInvocationError) as captured:
        invoke(http_target(handler))

    assert captured.value.code is code
    assert captured.value.retryable is True
    assert "private transport detail" not in str(captured.value)


def test_http_target_requires_credential_without_starting_transport(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABRIDGE_API_KEY", raising=False)
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return json_response(success_body())

    with raises(TargetInvocationError) as captured:
        invoke(http_target(handler))

    assert calls == 0
    assert captured.value.code is FailureCode.TARGET_AUTHENTICATION
    assert captured.value.retryable is False


def test_http_target_rejects_expectations_before_network(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABRIDGE_API_KEY", _SECRET)
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return json_response(success_body())

    target = http_target(handler)
    with raises(TargetInvocationError) as captured:
        invoke(target, query_request(expected_sql="SELECT 1"))

    assert calls == 0
    assert captured.value.code is FailureCode.TARGET_REJECTED


@mark.parametrize(
    "input_value",
    [
        "not-an-object",
        {"question": "", "chat_history": "", "language": "en"},
        {"question": "x" * 1_001, "chat_history": "", "language": "en"},
        {"question": "valid", "chat_history": "x" * 4_001, "language": "en"},
        {"question": "valid", "chat_history": "", "language": "fr"},
    ],
)
def test_http_target_rejects_invalid_request_contract_before_network(
    monkeypatch: MonkeyPatch,
    input_value: object,
) -> None:
    monkeypatch.setenv("DATABRIDGE_API_KEY", _SECRET)
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return json_response(success_body())

    request = TargetRequest(
        case_id="invalid-input",
        input=CanonicalJson.from_value(input_value),
    )
    with raises(TargetInvocationError) as captured:
        invoke(http_target(handler), request)

    assert calls == 0
    assert captured.value.code is FailureCode.TARGET_REJECTED


@mark.parametrize(
    "base_url",
    [
        "http://databridge.example",
        "http://databridge.example?key=value",
        "https://user:password@databridge.example",
        "https://databridge.example?key=value",
        "https://databridge.example#fragment",
        "https://databridge.example/prefix",
        "ftp://databridge.example",
        "relative-host",
    ],
)
def test_http_target_rejects_unsafe_base_urls(base_url: str) -> None:
    with raises(ValueError):
        DataBridgeHttpTarget(
            base_url=base_url,
            api_key_env="DATABRIDGE_API_KEY",
            synthetic_database_confirmed=True,
        )


def test_http_target_allows_insecure_loopback_only_when_explicit() -> None:
    for url in ("http://localhost:8000", "http://127.0.0.1", "http://[::1]"):
        with raises(ValueError, match="requires HTTPS"):
            DataBridgeHttpTarget(
                base_url=url,
                api_key_env="DATABRIDGE_API_KEY",
                synthetic_database_confirmed=True,
            )
        target = DataBridgeHttpTarget(
            base_url=url,
            api_key_env="DATABRIDGE_API_KEY",
            synthetic_database_confirmed=True,
            allow_insecure_loopback=True,
        )
        assert target.ref.digest is not None

    with raises(ValueError, match="requires HTTPS"):
        DataBridgeHttpTarget(
            base_url="http://databridge.example",
            api_key_env="DATABRIDGE_API_KEY",
            synthetic_database_confirmed=True,
            allow_insecure_loopback=True,
        )


@mark.parametrize(
    "options",
    [
        {"synthetic_database_confirmed": False},
        {"api_key_env": "PRIVATE-KEY"},
        {"timeout_seconds": 0},
        {"timeout_seconds": 61},
        {"timeout_seconds": float("nan")},
        {"max_response_bytes": 0},
        {"max_response_bytes": 262_145},
    ],
)
def test_http_target_rejects_unsafe_configuration(options: dict[str, object]) -> None:
    kwargs: dict[str, object] = {
        "base_url": "https://databridge.example",
        "api_key_env": "DATABRIDGE_API_KEY",
        "synthetic_database_confirmed": True,
        **options,
    }
    with raises(ValueError):
        DataBridgeHttpTarget(**kwargs)  # type: ignore[arg-type]


def test_http_target_identity_is_resolved_stable_and_secret_free(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABRIDGE_API_KEY", _SECRET)
    first = http_target(lambda _: json_response(success_body()))
    second = http_target(lambda _: json_response(success_body()))

    assert first.ref == second.ref
    assert first.ref.kind is ArtifactKind.TARGET
    assert first.ref.digest is not None
    assert _SECRET not in repr(first)
    assert "DATABRIDGE_API_KEY" in repr(first)
    assert _SECRET not in first.ref.model_dump_json()


def mock_fixtures() -> dict[str, object]:
    return {
        "query": {"status": 200, "body": success_body()},
        "clarify": {
            "status": 200,
            "body": success_body(status="clarification_required", executions=[]),
        },
        "refuse": {"status": 403},
    }


def test_mock_target_uses_same_normalizer_for_all_semantic_kinds() -> None:
    target = DataBridgeMockTarget(fixtures=mock_fixtures())

    query = invoke(target, query_request("query"))
    clarification = invoke(target, query_request("clarify"))
    refusal = invoke(target, query_request("refuse"))

    assert isinstance(query, TargetResponse)
    assert query.output.to_value() == {
        "kind": "query",
        "schema_version": "1",
        "sql_executions": ["SELECT count(*) FROM users"],
    }
    assert isinstance(clarification, TargetResponse)
    assert clarification.output.to_value() == {
        "clarification_code": "provider_clarification",
        "kind": "clarification",
        "schema_version": "1",
    }
    assert isinstance(refusal, TargetResponse)
    assert refusal.outcome is TargetOutcome.REFUSED
    assert refusal.refusal_code == "policy_block"
    assert refusal.usage.total_units == 0
    assert target.invocations == ("query", "clarify", "refuse")


def test_mock_target_has_zero_network_capability(monkeypatch: MonkeyPatch) -> None:
    def fail_client(*_: object, **__: object) -> NoReturn:
        raise AssertionError("mock target must not construct an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", fail_client)
    result = invoke(
        DataBridgeMockTarget(fixtures=mock_fixtures()),
        query_request("query"),
    )

    assert isinstance(result, TargetResponse)


def test_mock_target_digest_covers_content_not_authoring_order() -> None:
    fixtures = mock_fixtures()
    reordered = dict(reversed(tuple(fixtures.items())))

    first = DataBridgeMockTarget(fixtures=fixtures)
    second = DataBridgeMockTarget(fixtures=reordered)
    changed = DataBridgeMockTarget(fixtures={"refuse": {"status": 403}})

    assert first.ref.digest == second.ref.digest
    assert first.ref.digest != changed.ref.digest


@mark.parametrize(
    "fixtures",
    [
        {"../../unsafe": {"status": 403}},
        {"case": {"status": 500}},
        {"case": {"status": 403, "body": {}}},
        {"case": {"status": 200}},
        {"case": {"status": 200, "body": {**success_body(), "input_tokens": -1}}},
    ],
)
def test_mock_target_rejects_invalid_fixtures_without_echoing_them(
    fixtures: dict[str, object],
) -> None:
    with raises(ValueError, match="failed contract validation") as captured:
        DataBridgeMockTarget(fixtures=fixtures)

    assert "unsafe" not in str(captured.value)


def test_mock_target_rejects_unknown_cases_and_invalid_input_safely() -> None:
    target = DataBridgeMockTarget(fixtures=mock_fixtures())

    for item in (
        query_request("missing"),
        query_request("query", expected="private expectation"),
    ):
        with raises(TargetInvocationError) as captured:
            invoke(target, item)
        assert captured.value.code is FailureCode.TARGET_REJECTED
        assert "private" not in str(captured.value)
