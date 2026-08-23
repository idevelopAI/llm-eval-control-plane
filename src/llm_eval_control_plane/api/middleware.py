"""Raw ASGI request hardening shared by every API v1 route."""

from __future__ import annotations

import re
import secrets

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from llm_eval_control_plane.domain.canonical import CanonicalJsonError, parse_json

_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BODY_METHODS = frozenset({"PATCH", "POST", "PUT"})


def request_id_from_scope(scope: Scope) -> str:
    state = scope.get("state")
    if isinstance(state, dict):
        value = state.get("request_id")
        if isinstance(value, str):
            return value
    return "request_unknown"


def error_document(
    *,
    code: str,
    message: str,
    request_id: str,
    details: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "api-error/v1",
        "error": {
            "code": code,
            "details": details or [],
            "message": message,
            "request_id": request_id,
        },
    }


class ApiBoundaryMiddleware:
    """Limit and strictly validate JSON before framework deserialization."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        if type(max_body_bytes) is not int or max_body_bytes <= 0:
            raise ValueError("Maximum request body size must be positive")
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        request_id = self._request_id(headers)
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-request-id"
                ]
                response_headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = response_headers
            await send(message)

        if scope.get("method") in _BODY_METHODS:
            failure, buffered = await self._validated_body(
                headers=headers,
                receive=receive,
                request_id=request_id,
            )
            if failure is not None:
                await failure(scope, receive, send_with_request_id)
                return
            assert buffered is not None
            delivered = False

            async def replay() -> Message:
                nonlocal delivered
                if delivered:
                    return {"type": "http.disconnect"}
                delivered = True
                return {
                    "type": "http.request",
                    "body": buffered,
                    "more_body": False,
                }

            receive = replay

        await self._app(scope, receive, send_with_request_id)

    async def _validated_body(
        self,
        *,
        headers: list[tuple[bytes, bytes]],
        receive: Receive,
        request_id: str,
    ) -> tuple[JSONResponse | None, bytes | None]:
        content_lengths = self._header_values(headers, b"content-length")
        if len(content_lengths) > 1:
            return self._error(
                status=400,
                code="invalid_request",
                message="Request headers are invalid",
                request_id=request_id,
            ), None
        if content_lengths:
            try:
                declared_size = int(content_lengths[0].decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                declared_size = -1
            if declared_size < 0:
                return self._error(
                    status=400,
                    code="invalid_request",
                    message="Request headers are invalid",
                    request_id=request_id,
                ), None
            if declared_size > self._max_body_bytes:
                return self._error(
                    status=413,
                    code="request_body_too_large",
                    message="Request body exceeds the configured limit",
                    request_id=request_id,
                ), None

        if self._header_values(headers, b"content-encoding"):
            return self._error(
                status=415,
                code="unsupported_content_encoding",
                message="Encoded request bodies are not supported",
                request_id=request_id,
            ), None

        content_types = self._header_values(headers, b"content-type")
        if len(content_types) != 1 or not self._is_json_content_type(content_types[0]):
            return self._error(
                status=415,
                code="unsupported_media_type",
                message="Content-Type must be application/json",
                request_id=request_id,
            ), None

        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return self._error(
                    status=400,
                    code="invalid_request",
                    message="Request body could not be read",
                    request_id=request_id,
                ), None
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > self._max_body_bytes:
                return self._error(
                    status=413,
                    code="request_body_too_large",
                    message="Request body exceeds the configured limit",
                    request_id=request_id,
                ), None
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        try:
            parse_json(body.decode("utf-8"))
        except (CanonicalJsonError, RecursionError, UnicodeDecodeError):
            return self._error(
                status=400,
                code="invalid_json",
                message="Request body is not valid strict JSON",
                request_id=request_id,
            ), None
        return None, body

    @staticmethod
    def _header_values(
        headers: list[tuple[bytes, bytes]],
        name: bytes,
    ) -> list[bytes]:
        return [value for key, value in headers if key.lower() == name]

    @staticmethod
    def _is_json_content_type(value: bytes) -> bool:
        try:
            parts = [part.strip().lower() for part in value.decode("ascii").split(";")]
        except UnicodeDecodeError:
            return False
        if not parts or parts[0] != "application/json":
            return False
        return all(part in {"", "charset=utf-8"} for part in parts[1:])

    @staticmethod
    def _request_id(headers: list[tuple[bytes, bytes]]) -> str:
        values = ApiBoundaryMiddleware._header_values(headers, b"x-request-id")
        if len(values) == 1:
            try:
                candidate = values[0].decode("ascii")
            except UnicodeDecodeError:
                candidate = ""
            if _REQUEST_ID.fullmatch(candidate):
                return candidate
        return f"req_{secrets.token_hex(16)}"

    @staticmethod
    def _error(
        *,
        status: int,
        code: str,
        message: str,
        request_id: str,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content=error_document(
                code=code,
                message=message,
                request_id=request_id,
            ),
        )


__all__ = ["ApiBoundaryMiddleware", "error_document", "request_id_from_scope"]
