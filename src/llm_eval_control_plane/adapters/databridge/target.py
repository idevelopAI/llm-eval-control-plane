"""Live and deterministic-mock target adapters for DataBridge v1.2.0."""

from __future__ import annotations

import ipaddress
import math
import os
import re
from collections.abc import Mapping
from typing import Final

import httpx
from pydantic import TypeAdapter, ValidationError

from llm_eval_control_plane.adapters.databridge.contracts import (
    DataBridgeInput,
    DataBridgeMockFixture,
    DataBridgeMockRefusal,
    DataBridgeMockSuccess,
    DataBridgeQueryResponse,
)
from llm_eval_control_plane.application import TargetInvocationError
from llm_eval_control_plane.domain import (
    ArtifactKind,
    ArtifactRef,
    CanonicalJson,
    FailureCode,
    TargetOutcome,
    TargetRequest,
    TargetResponse,
    TokenUsage,
    sha256_digest,
)
from llm_eval_control_plane.domain.canonical import (
    CanonicalJsonError,
    JsonValue,
    canonical_json_bytes,
    parse_json,
)
from llm_eval_control_plane.domain.datasets import CaseId

_CASE_ID_ADAPTER = TypeAdapter(CaseId)
_MOCK_FIXTURE_ADAPTER: TypeAdapter[DataBridgeMockFixture] = TypeAdapter(
    DataBridgeMockFixture
)
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


class DataBridgeHttpTarget:
    """Call a synthetic-database DataBridge deployment over bounded HTTPS."""

    _CONTRACT_VERSION: Final = "databridge-http/v1.2.0"
    _ENDPOINT_PATH: Final = "/api/v1/query"
    _HARD_MAX_RESPONSE_BYTES: Final = 256 * 1_024

    def __init__(
        self,
        *,
        base_url: str,
        api_key_env: str,
        synthetic_database_confirmed: bool,
        name: str = "databridge/live",
        revision: int = 1,
        timeout_seconds: float = 15.0,
        max_response_bytes: int = _HARD_MAX_RESPONSE_BYTES,
        allow_insecure_loopback: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if synthetic_database_confirmed is not True:
            raise ValueError("Live DataBridge requires a confirmed synthetic database")
        endpoint = self._validated_endpoint(base_url, allow_insecure_loopback)
        if not _ENV_NAME.fullmatch(api_key_env):
            raise ValueError("DataBridge credential environment name is invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60
        ):
            raise ValueError("DataBridge timeout must be within 0 and 60 seconds")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 1 <= max_response_bytes <= self._HARD_MAX_RESPONSE_BYTES
        ):
            raise ValueError("DataBridge response limit must be within 256 KiB")

        self._endpoint = endpoint
        self._api_key_env = api_key_env
        self._timeout_seconds = float(timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._transport = transport
        self._ref = ArtifactRef(
            kind=ArtifactKind.TARGET,
            name=name,
            revision=revision,
            digest=sha256_digest(
                {
                    "api_key_env": api_key_env,
                    "allow_insecure_loopback": allow_insecure_loopback,
                    "base_url": str(endpoint.copy_with(path="/")),
                    "contract": self._CONTRACT_VERSION,
                    "endpoint": self._ENDPOINT_PATH,
                    "max_response_bytes": max_response_bytes,
                    "mode": "live",
                    "synthetic_database_confirmed": True,
                    "timeout_seconds": self._timeout_seconds,
                }
            ),
        )

    @property
    def ref(self) -> ArtifactRef:
        return self._ref

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(ref={self._ref!r}, "
            f"endpoint={str(self._endpoint)!r}, "
            f"api_key_env={self._api_key_env!r})"
        )

    async def invoke(self, request: TargetRequest) -> object:
        payload = _validated_input(request)
        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            raise _invocation_error(FailureCode.TARGET_AUTHENTICATION)

        request_bytes = canonical_json_bytes(payload.model_dump(mode="json"))
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        }
        timeout = httpx.Timeout(self._timeout_seconds)
        try:
            async with (
                httpx.AsyncClient(
                    follow_redirects=False,
                    timeout=timeout,
                    transport=self._transport,
                    trust_env=False,
                    verify=True,
                ) as client,
                client.stream(
                    "POST",
                    self._endpoint,
                    content=request_bytes,
                    headers=headers,
                ) as response,
            ):
                return await self._map_response(response)
        except TargetInvocationError:
            raise
        except httpx.TimeoutException:
            raise _invocation_error(FailureCode.TARGET_TIMEOUT) from None
        except httpx.TransportError:
            raise _invocation_error(FailureCode.TARGET_UNAVAILABLE) from None

    async def _map_response(self, response: httpx.Response) -> TargetResponse:
        status = response.status_code
        if status == 403:
            return _refusal_response()
        failure = _status_failure(status)
        if failure is not None:
            raise failure

        content_type = response.headers.get("content-type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise _invocation_error(FailureCode.TARGET_PROTOCOL_ERROR)
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                raise _invocation_error(FailureCode.TARGET_PROTOCOL_ERROR) from None
            if declared_length < 0 or declared_length > self._max_response_bytes:
                raise _invocation_error(FailureCode.TARGET_PROTOCOL_ERROR)

        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self._max_response_bytes:
                raise _invocation_error(FailureCode.TARGET_PROTOCOL_ERROR)
            chunks.append(chunk)
        try:
            parsed = parse_json(b"".join(chunks).decode("utf-8"))
            wire = DataBridgeQueryResponse.model_validate_json(
                canonical_json_bytes(parsed),
                strict=True,
            )
        except (
            CanonicalJsonError,
            UnicodeDecodeError,
            ValidationError,
            ValueError,
        ):
            raise _invocation_error(FailureCode.TARGET_PROTOCOL_ERROR) from None
        return _normalized_success(wire)

    @classmethod
    def _validated_endpoint(
        cls,
        base_url: str,
        allow_insecure_loopback: bool,
    ) -> httpx.URL:
        try:
            url = httpx.URL(base_url)
        except (TypeError, ValueError):
            raise ValueError("DataBridge base URL is invalid") from None
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError(
                "DataBridge base URL must be an origin without credentials"
            )
        if url.scheme == "http" and not (
            allow_insecure_loopback and _is_loopback(url.host)
        ):
            raise ValueError("DataBridge live traffic requires HTTPS")
        return url.copy_with(path=cls._ENDPOINT_PATH)


class DataBridgeMockTarget:
    """Replay strict DataBridge wire fixtures without any network capability."""

    _CONTRACT_VERSION: Final = "databridge-mock/v1.2.0"

    def __init__(
        self,
        *,
        fixtures: Mapping[str, object],
        name: str = "databridge/mock",
        revision: int = 1,
    ) -> None:
        normalized = self._validated_fixtures(fixtures)
        fixture_content = {
            case_id: fixture.model_dump(mode="json", exclude_none=True)
            for case_id, fixture in normalized.items()
        }
        self._fixtures = normalized
        self._invocations: list[str] = []
        self._ref = ArtifactRef(
            kind=ArtifactKind.TARGET,
            name=name,
            revision=revision,
            digest=sha256_digest(
                {
                    "contract": self._CONTRACT_VERSION,
                    "fixtures": fixture_content,
                    "mode": "mock",
                }
            ),
        )

    @property
    def ref(self) -> ArtifactRef:
        return self._ref

    @property
    def invocations(self) -> tuple[str, ...]:
        return tuple(self._invocations)

    async def invoke(self, request: TargetRequest) -> object:
        _validated_input(request)
        self._invocations.append(request.case_id)
        fixture = self._fixtures.get(request.case_id)
        if fixture is None:
            raise _invocation_error(FailureCode.TARGET_REJECTED)
        if isinstance(fixture, DataBridgeMockRefusal):
            return _refusal_response()
        return _normalized_success(fixture.body)

    @staticmethod
    def _validated_fixtures(
        fixtures: Mapping[str, object],
    ) -> dict[str, DataBridgeMockSuccess | DataBridgeMockRefusal]:
        normalized: dict[str, DataBridgeMockSuccess | DataBridgeMockRefusal] = {}
        try:
            for case_id, fixture in fixtures.items():
                validated_case_id = _CASE_ID_ADAPTER.validate_python(
                    case_id,
                    strict=True,
                )
                normalized[validated_case_id] = _MOCK_FIXTURE_ADAPTER.validate_python(
                    fixture
                )
        except (ValidationError, ValueError, TypeError):
            raise ValueError(
                "DataBridge mock fixtures failed contract validation"
            ) from None
        return dict(sorted(normalized.items()))


def _validated_input(request: TargetRequest) -> DataBridgeInput:
    try:
        return DataBridgeInput.model_validate(request.input.to_value(), strict=True)
    except (ValidationError, ValueError, TypeError):
        raise _invocation_error(FailureCode.TARGET_REJECTED) from None


def _normalized_success(wire: DataBridgeQueryResponse) -> TargetResponse:
    if wire.status == "answered":
        output: dict[str, JsonValue] = {
            "kind": "query",
            "schema_version": "1",
            "sql_executions": [execution.sql for execution in wire.executions],
        }
    else:
        output = {
            "clarification_code": "provider_clarification",
            "kind": "clarification",
            "schema_version": "1",
        }
    return TargetResponse(
        output=CanonicalJson.from_value(output),
        outcome=TargetOutcome.COMPLETED,
        usage=TokenUsage(
            input_units=wire.input_tokens,
            output_units=wire.output_tokens,
        ),
    )


def _refusal_response() -> TargetResponse:
    return TargetResponse(
        output=CanonicalJson.from_value({"kind": "refusal", "schema_version": "1"}),
        outcome=TargetOutcome.REFUSED,
        refusal_code="policy_block",
        usage=TokenUsage(input_units=0, output_units=0),
    )


def _status_failure(status: int) -> TargetInvocationError | None:
    if status == 200:
        return None
    if status == 401:
        return _invocation_error(FailureCode.TARGET_AUTHENTICATION)
    if status == 429:
        return _invocation_error(FailureCode.TARGET_RATE_LIMITED)
    if status in {408, 504}:
        return _invocation_error(FailureCode.TARGET_TIMEOUT)
    if 500 <= status <= 599:
        return _invocation_error(FailureCode.TARGET_UNAVAILABLE)
    if 400 <= status <= 499:
        return _invocation_error(FailureCode.TARGET_REJECTED)
    return _invocation_error(FailureCode.TARGET_PROTOCOL_ERROR)


def _invocation_error(code: FailureCode) -> TargetInvocationError:
    return TargetInvocationError(
        code=code,
        retryable=code
        in {
            FailureCode.TARGET_RATE_LIMITED,
            FailureCode.TARGET_TIMEOUT,
            FailureCode.TARGET_UNAVAILABLE,
        },
    )


def _is_loopback(host: str) -> bool:
    normalized = host.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False
