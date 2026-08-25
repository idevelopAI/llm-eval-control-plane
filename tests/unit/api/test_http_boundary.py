import asyncio
import base64
from typing import cast

from fastapi.testclient import TestClient
from pytest import raises
from starlette.types import Message, Receive, Scope, Send

from llm_eval_control_plane.api.app import create_app
from llm_eval_control_plane.api.middleware import (
    ApiBoundaryMiddleware,
    request_id_from_scope,
)
from llm_eval_control_plane.api.security import ControlPlaneScope

from .conftest import (
    AUTH_HEADERS,
    ApiHarness,
    build_authorizer,
    build_telemetry,
)

_AUTH_HEADER_BYTES = [
    (name.lower().encode("ascii"), value.encode("ascii"))
    for name, value in AUTH_HEADERS.items()
]


def test_idempotency_header_is_required_and_never_reflected(
    api_harness: ApiHarness,
    run_body: dict[str, object],
) -> None:
    missing = api_harness.client.post("/v1/runs", json=run_body)
    invalid = api_harness.client.post(
        "/v1/runs",
        json=run_body,
        headers={"Idempotency-Key": "private sentinel"},
    )

    assert missing.status_code == invalid.status_code == 422
    assert missing.json()["error"]["code"] == "invalid_request"
    assert "private sentinel" not in invalid.text
    assert invalid.json()["error"]["details"] == [
        {
            "location": ["header", "idempotency-key"],
            "type": "string_pattern_mismatch",
        }
    ]


def test_authentication_precedes_json_parsing_and_health_stays_public(
    api_harness: ApiHarness,
) -> None:
    sentinel = b"private-unauthenticated-body-sentinel"
    with TestClient(
        create_app(
            service=api_harness.service,
            authorizer=build_authorizer(),
            telemetry=build_telemetry(),
        ),
        raise_server_exceptions=False,
    ) as client:
        missing = client.post(
            "/v1/datasets",
            content=b'{"broken":"' + sentinel,
            headers={"Content-Type": "application/json"},
        )
        wrong_project = client.get(
            "/v1/jobs",
            headers={
                "Authorization": AUTH_HEADERS["Authorization"],
                "X-Project-ID": "project-other",
            },
        )
        health = client.get("/health/live")

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "authentication_required"
    assert missing.headers["www-authenticate"] == "Bearer"
    assert sentinel.decode() not in missing.text
    assert wrong_project.status_code == 403
    assert wrong_project.json()["error"]["code"] == "permission_denied"
    assert health.status_code == 200


def test_read_and_observability_scopes_are_enforced_independently(
    api_harness: ApiHarness,
) -> None:
    with TestClient(
        create_app(
            service=api_harness.service,
            authorizer=build_authorizer((ControlPlaneScope.READ,)),
            telemetry=build_telemetry(),
        ),
        headers=AUTH_HEADERS,
        raise_server_exceptions=False,
    ) as reader:
        listed = reader.get("/v1/jobs")
        mutation = reader.post(
            "/v1/datasets",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        canceled = reader.post(
            "/v1/jobs/missing/cancellation",
            json={"reason": "requested"},
        )
        reader_metrics = reader.get("/metrics")

    with TestClient(
        create_app(
            service=api_harness.service,
            authorizer=build_authorizer((ControlPlaneScope.OBSERVABILITY_READ,)),
            telemetry=build_telemetry(),
        ),
        headers=AUTH_HEADERS,
        raise_server_exceptions=False,
    ) as observer:
        metrics = observer.get("/metrics")
        observer_api = observer.get("/v1/jobs")

    assert listed.status_code == 200
    assert mutation.status_code == canceled.status_code == 403
    assert reader_metrics.status_code == 403
    assert metrics.status_code == 200
    assert metrics.headers["content-type"].startswith("text/plain")
    assert b"control_plane_http_requests_total" in metrics.content
    assert observer_api.status_code == 403


def test_strict_json_rejects_duplicate_keys_non_finite_numbers_and_bom(
    api_harness: ApiHarness,
) -> None:
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": "strict-json",
    }
    bodies = (
        b'{"dataset_name":"one","dataset_name":"private-sentinel"}',
        b'{"dataset_name":NaN}',
        b'\xef\xbb\xbf{"dataset_name":"fixture"}',
        b'{"dataset_name":"\xff"}',
        b'{"dataset_name":',
    )

    for body in bodies:
        response = api_harness.client.post("/v1/runs", content=body, headers=headers)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_json"
        assert "private-sentinel" not in response.text


def test_deeply_nested_json_is_a_safe_client_error_with_request_id(
    api_harness: ApiHarness,
) -> None:
    body = (
        b'{"name":"x","revision":1,"cases":'
        + (b"[" * 1_100)
        + b"0"
        + (b"]" * 1_100)
        + b"}"
    )

    response = api_harness.client.post(
        "/v1/datasets",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Request-ID": "nested-request",
        },
    )

    assert response.status_code == 400
    request_id = response.headers["x-request-id"]
    assert request_id.startswith("req_")
    assert response.json()["error"]["code"] == "invalid_json"
    assert response.json()["error"]["request_id"] == request_id
    assert "nested-request" not in response.text


def test_oversized_json_integer_is_a_safe_client_error_with_request_id(
    api_harness: ApiHarness,
) -> None:
    response = api_harness.client.post(
        "/v1/datasets",
        content=b'{"name":' + (b"9" * 10_000) + b"}",
        headers={
            "Content-Type": "application/json",
            "X-Request-ID": "integer-limit-request",
        },
    )

    assert response.status_code == 400
    request_id = response.headers["x-request-id"]
    assert request_id.startswith("req_")
    assert response.json()["error"] == {
        "code": "invalid_json",
        "details": [],
        "message": "Request body is not valid strict JSON",
        "request_id": request_id,
    }
    assert "9999999999999999" not in response.text
    assert "integer-limit-request" not in response.text


def test_media_type_and_content_encoding_are_enforced(
    api_harness: ApiHarness,
) -> None:
    media = api_harness.client.post(
        "/v1/datasets",
        content=b"{}",
        headers={"Content-Type": "text/plain"},
    )
    encoding = api_harness.client.post(
        "/v1/datasets",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )

    assert media.status_code == encoding.status_code == 415
    assert media.json()["error"]["code"] == "unsupported_media_type"
    assert encoding.json()["error"]["code"] == "unsupported_content_encoding"


def test_body_limit_and_collection_bounds_are_stable(
    api_harness: ApiHarness,
    dataset_body: dict[str, object],
) -> None:
    with TestClient(
        create_app(
            service=api_harness.service,
            authorizer=build_authorizer(),
            telemetry=build_telemetry(),
            max_body_bytes=32,
        ),
        headers=AUTH_HEADERS,
        raise_server_exceptions=False,
    ) as client:
        response = client.post(
            "/v1/datasets",
            content=b'{"padding":"' + (b"x" * 64) + b'"}',
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_body_too_large"

    too_many_cases = {**dataset_body, "cases": [{}] * 1_001}
    cases = api_harness.client.post("/v1/datasets", json=too_many_cases)
    too_many_overrides = {
        "dataset_name": "fixture",
        "dataset_revision": 1,
        "evaluators": ["exact_match"],
        "scenario_overrides": {f"case-{index}": "echo" for index in range(1_001)},
    }
    overrides = api_harness.client.post(
        "/v1/runs",
        json=too_many_overrides,
        headers={"Idempotency-Key": "too-many-overrides"},
    )

    assert cases.status_code == overrides.status_code == 422
    assert api_harness.client.get("/v1/jobs?limit=101").status_code == 422

    too_many_slices = {
        **dataset_body,
        "revision": 2,
        "cases": [
            {
                "case_id": "many-slices",
                "input": {"scenario": "echo", "value": "answer"},
                "slices": [f"slice-{index}" for index in range(33)],
            }
        ],
    }
    slices = api_harness.client.post("/v1/datasets", json=too_many_slices)
    assert slices.status_code == 422

    invalid_schema = {
        "name": "invalid-schema",
        "revision": 1,
        "cases": [
            {
                "case_id": "invalid-schema",
                "input": {"scenario": "echo"},
                "expected_schema": "not-an-object",
            }
        ],
    }
    semantic = api_harness.client.post("/v1/datasets", json=invalid_schema)
    duplicate_evaluators = api_harness.client.post(
        "/v1/runs",
        json={
            "dataset_name": "fixture",
            "dataset_revision": 1,
            "evaluators": ["exact_match", "exact_match"],
        },
        headers={"Idempotency-Key": "duplicate-evaluators"},
    )
    assert semantic.status_code == duplicate_evaluators.status_code == 422

    duplicate_slices = {
        **dataset_body,
        "revision": 3,
        "cases": [
            {
                "case_id": "duplicate-slices",
                "input": {"scenario": "echo"},
                "slices": ["same", "same"],
            }
        ],
    }
    unique_slice_overflow = {
        **dataset_body,
        "revision": 4,
        "cases": [
            {
                "case_id": f"slice-case-{index}",
                "input": {"scenario": "echo"},
                "slices": [f"slice-{index}"],
            }
            for index in range(129)
        ],
    }
    duplicate = api_harness.client.post("/v1/datasets", json=duplicate_slices)
    overflow = api_harness.client.post("/v1/datasets", json=unique_slice_overflow)
    assert duplicate.status_code == overflow.status_code == 422


def test_request_id_is_generated_without_retaining_caller_values(
    api_harness: ApiHarness,
) -> None:
    caller_value = "cpk_" + ("A" * 43)
    credential_shaped = api_harness.client.get(
        "/missing",
        headers={"X-Request-ID": caller_value},
    )
    malformed = api_harness.client.get(
        "/missing",
        headers={"X-Request-ID": "private sentinel\nvalue"},
    )

    for response in (credential_shaped, malformed):
        request_id = response.headers["x-request-id"]
        assert request_id.startswith("req_") and len(request_id) == 36
        assert response.json()["error"]["request_id"] == request_id
    assert caller_value not in credential_shaped.text
    assert "private sentinel" not in malformed.text


def test_method_not_allowed_preserves_the_safe_allow_header(
    api_harness: ApiHarness,
) -> None:
    response = api_harness.client.post("/health/live", json={})

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_chunked_strict_json_and_size_checks_cover_the_full_stream(
    api_harness: ApiHarness,
) -> None:
    async def invoke(
        chunks: tuple[bytes, ...],
        *,
        limit: int,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> list[Message]:
        app = create_app(
            service=api_harness.service,
            authorizer=build_authorizer(),
            telemetry=build_telemetry(),
            max_body_bytes=limit,
        )
        messages: list[Message] = [
            {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(chunks) - 1,
            }
            for index, chunk in enumerate(chunks)
        ]
        sent: list[Message] = []

        async def receive() -> Message:
            return messages.pop(0)

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/datasets",
            "raw_path": b"/v1/datasets",
            "query_string": b"",
            "headers": [
                *(headers or [(b"content-type", b"application/json")]),
                *_AUTH_HEADER_BYTES,
            ],
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
            "root_path": "",
        }
        await app(scope, receive, send)
        return sent

    duplicate = asyncio.run(invoke((b'{"name":"one",', b'"name":"two"}'), limit=1_024))
    oversized = asyncio.run(invoke((b'{"x":"', b"z" * 64, b'"}'), limit=32))
    duplicate_length = asyncio.run(
        invoke(
            (b"{}",),
            limit=32,
            headers=[
                (b"content-type", b"application/json"),
                (b"content-length", b"2"),
                (b"content-length", b"2"),
            ],
        )
    )
    invalid_length = asyncio.run(
        invoke(
            (b"{}",),
            limit=32,
            headers=[
                (b"content-type", b"application/json"),
                (b"content-length", b"invalid"),
            ],
        )
    )

    assert duplicate[0]["status"] == 400
    assert oversized[0]["status"] == 413
    assert duplicate_length[0]["status"] == 400
    assert invalid_length[0]["status"] == 400


def test_boundary_handles_non_http_scopes_and_replays_one_bounded_body() -> None:
    captured: list[object] = []
    sent: list[Message] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        captured.append(scope["type"])
        if scope["type"] != "http":
            return
        first = await receive()
        second = await receive()
        captured.extend((first, second))
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [(b"x-request-id", b"downstream-value")],
            }
        )
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    middleware = ApiBoundaryMiddleware(
        downstream,
        max_body_bytes=32,
        authorizer=build_authorizer(),
    )

    async def invoke() -> None:
        async def receive() -> Message:
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message: Message) -> None:
            sent.append(message)

        lifespan = cast(Scope, {"type": "lifespan"})
        await middleware(lifespan, receive, send)
        http = cast(
            Scope,
            {
                "type": "http",
                "method": "POST",
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"x-request-id", b"boundary-request"),
                ],
            },
        )
        await middleware(http, receive, send)

    asyncio.run(invoke())

    assert captured[0] == "lifespan"
    assert captured[1] == "http"
    assert cast(dict[str, object], captured[2])["body"] == b"{}"
    assert cast(dict[str, object], captured[3])["type"] == "http.disconnect"
    response_headers = cast(list[tuple[bytes, bytes]], sent[0]["headers"])
    assert len(response_headers) == 1
    assert response_headers[0][0] == b"x-request-id"
    assert response_headers[0][1].startswith(b"req_")
    assert response_headers[0][1] != b"boundary-request"


def test_boundary_helpers_fail_closed_on_invalid_protocol_values() -> None:
    async def downstream(_scope: Scope, _receive: Receive, _send: Send) -> None:
        return None

    for value in (0, -1, True):
        with raises(ValueError, match="body size"):
            ApiBoundaryMiddleware(
                downstream,
                max_body_bytes=value,
                authorizer=build_authorizer(),
            )

    assert request_id_from_scope(cast(Scope, {})) == "request_unknown"
    assert ApiBoundaryMiddleware._is_json_content_type(b"\xff") is False
    assert (
        ApiBoundaryMiddleware._is_json_content_type(b"application/json; charset=utf-16")
        is False
    )
    generated = ApiBoundaryMiddleware._request_id([(b"x-request-id", b"\xff")])
    assert generated.startswith("req_")


def test_deeply_nested_cursor_is_a_stable_client_error(
    api_harness: ApiHarness,
) -> None:
    nested = ("[" * 760 + "0" + "]" * 760).encode("ascii")
    cursor = base64.urlsafe_b64encode(nested).decode("ascii").rstrip("=")
    assert len(cursor) <= 2_048

    response = api_harness.client.get("/v1/jobs", params={"cursor": cursor})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"
