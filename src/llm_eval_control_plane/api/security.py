"""Bounded bearer authentication and project authorization for HTTP scopes."""

from __future__ import annotations

import os
import re
import secrets
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.types import Scope

from llm_eval_control_plane.domain.canonical import CanonicalJsonError, parse_json

_MAX_CONFIGURATION_BYTES = 256 * 1024
_MAX_PRINCIPALS = 512
_MAX_AUTHORIZATION_HEADER_BYTES = 128
_MAX_PROJECT_HEADER_BYTES = 128
_TOKEN_PATTERN = re.compile(r"^cpk_[A-Za-z0-9_-]{43}$")
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_PRINCIPAL_STATE_KEY = "control_plane_principal"
_DUMMY_TOKEN = "cpk_" + ("A" * 43)


class SecurityConfigurationError(RuntimeError):
    """A content-free authentication configuration failure."""

    def __init__(self) -> None:
        super().__init__("Authentication configuration is invalid")


class ControlPlaneScope(StrEnum):
    """Explicit permissions accepted by the control-plane boundary."""

    READ = "control-plane:read"
    WRITE = "control-plane:write"
    CANCEL = "control-plane:cancel"
    OBSERVABILITY_READ = "observability:read"


class _ConfigurationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class PrincipalConfiguration(_ConfigurationModel):
    """One principal containing only a non-reversible credential digest."""

    principal_id: Annotated[
        str,
        Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN),
    ]
    token_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN, repr=False)]
    scopes: Annotated[
        tuple[ControlPlaneScope, ...],
        Field(min_length=1, max_length=len(ControlPlaneScope)),
    ]

    @model_validator(mode="after")
    def validate_scopes(self) -> Self:
        values = tuple(scope.value for scope in self.scopes)
        if len(values) != len(set(values)) or values != tuple(sorted(values)):
            raise ValueError("principal scopes must be unique and ordered")
        return self

    def __repr__(self) -> str:
        return "PrincipalConfiguration()"


class AuthenticationConfiguration(_ConfigurationModel):
    """Strict single-deployment project and principal registry."""

    schema_version: Literal["control-plane-auth/v1"]
    project_id: Annotated[
        str,
        Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN),
    ]
    principals: Annotated[
        tuple[PrincipalConfiguration, ...],
        Field(min_length=1, max_length=_MAX_PRINCIPALS),
    ]

    @model_validator(mode="after")
    def validate_principals(self) -> Self:
        principal_ids = tuple(item.principal_id for item in self.principals)
        token_digests = tuple(item.token_digest for item in self.principals)
        if len(principal_ids) != len(set(principal_ids)):
            raise ValueError("principal identities must be unique")
        if len(token_digests) != len(set(token_digests)):
            raise ValueError("principal credential digests must be unique")
        if principal_ids != tuple(sorted(principal_ids)):
            raise ValueError("principals must be ordered")
        return self

    def __repr__(self) -> str:
        return "AuthenticationConfiguration()"


@dataclass(frozen=True, slots=True, repr=False)
class AuthorizedPrincipal:
    """Safe principal metadata retained for the remainder of one request."""

    project_id: str
    principal_id: str
    scopes: frozenset[ControlPlaneScope]

    def __repr__(self) -> str:
        return "AuthorizedPrincipal()"


@dataclass(frozen=True, slots=True, repr=False)
class AuthorizationFailure:
    """Content-free failure ready for translation to the public error envelope."""

    status: int
    code: str
    message: str
    headers: Mapping[str, str] = field(compare=False)

    def __repr__(self) -> str:
        return "AuthorizationFailure()"


_UNAUTHORIZED = AuthorizationFailure(
    status=401,
    code="authentication_required",
    message="Authentication credentials are invalid",
    headers=MappingProxyType({"WWW-Authenticate": "Bearer"}),
)
_FORBIDDEN = AuthorizationFailure(
    status=403,
    code="permission_denied",
    message="Permission is not granted",
    headers=MappingProxyType({}),
)


class ControlPlaneAuthorizer:
    """Authenticate bounded bearer tokens and enforce one project boundary."""

    __slots__ = ("_configuration",)

    def __init__(self, configuration: AuthenticationConfiguration) -> None:
        if not isinstance(configuration, AuthenticationConfiguration):
            raise TypeError("authentication configuration is required")
        self._configuration = configuration

    @classmethod
    def from_file(cls, path: Path) -> ControlPlaneAuthorizer:
        return cls(load_authentication_configuration(path))

    def __repr__(self) -> str:
        return "ControlPlaneAuthorizer()"

    def authorize(self, scope: Scope) -> AuthorizationFailure | None:
        """Authorize a protected ASGI scope and retain only safe metadata."""
        _clear_principal(scope)
        required_scope = _required_scope(scope)
        if required_scope is None:
            return None

        headers = _bounded_headers(scope)
        if headers is None:
            return _UNAUTHORIZED
        authorization_values = headers.get(b"authorization", ())
        token, token_is_valid = _presented_token(authorization_values)
        presented_digest = _token_digest_unchecked(
            token if token_is_valid else _DUMMY_TOKEN
        )

        matched: PrincipalConfiguration | None = None
        for principal in self._configuration.principals:
            if secrets.compare_digest(principal.token_digest, presented_digest):
                matched = principal
        if not token_is_valid or matched is None:
            return _UNAUTHORIZED

        project_values = headers.get(b"x-project-id", ())
        project_id = _presented_project(project_values)
        project_matches = project_id is not None and secrets.compare_digest(
            project_id,
            self._configuration.project_id,
        )
        if not project_matches or required_scope not in matched.scopes:
            return _FORBIDDEN

        state = scope.setdefault("state", {})
        if not isinstance(state, dict):
            return _UNAUTHORIZED
        state[_PRINCIPAL_STATE_KEY] = AuthorizedPrincipal(
            project_id=self._configuration.project_id,
            principal_id=matched.principal_id,
            scopes=frozenset(matched.scopes),
        )
        return None


def digest_token(token: str) -> str:
    """Return the persisted SHA-256 digest for one strictly formatted token."""
    if not isinstance(token, str) or not _canonical_token(token):
        raise ValueError("API credential is invalid")
    return _token_digest_unchecked(token)


def load_authentication_configuration(path: Path) -> AuthenticationConfiguration:
    """Read one bounded non-symlink regular file and validate its strict schema."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise SecurityConfigurationError from None
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise SecurityConfigurationError from None
    flags = (
        os.O_RDONLY
        | no_follow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_CONFIGURATION_BYTES
        ):
            raise OSError("configuration is not a bounded regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(_MAX_CONFIGURATION_BYTES + 1)
        if len(payload) > _MAX_CONFIGURATION_BYTES:
            raise OSError("configuration exceeds its size bound")
        parse_json(payload.decode("utf-8"))
        return AuthenticationConfiguration.model_validate_json(payload, strict=True)
    except (
        CanonicalJsonError,
        OSError,
        RecursionError,
        UnicodeError,
        ValidationError,
        ValueError,
    ):
        raise SecurityConfigurationError from None
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


def principal_from_scope(scope: Scope) -> AuthorizedPrincipal | None:
    """Return safe authenticated metadata previously retained in scope state."""
    state = scope.get("state")
    if not isinstance(state, dict):
        return None
    value = state.get(_PRINCIPAL_STATE_KEY)
    return value if isinstance(value, AuthorizedPrincipal) else None


def _clear_principal(scope: Scope) -> None:
    state = scope.get("state")
    if isinstance(state, dict):
        state.pop(_PRINCIPAL_STATE_KEY, None)


def _required_scope(scope: Scope) -> ControlPlaneScope | None:
    path = scope.get("path")
    if not isinstance(path, str):
        return None
    if path == "/metrics" or path.startswith("/metrics/"):
        return ControlPlaneScope.OBSERVABILITY_READ
    if path != "/v1" and not path.startswith("/v1/"):
        return None
    method = scope.get("method")
    if method in {"GET", "HEAD", "OPTIONS"}:
        return ControlPlaneScope.READ
    if (
        method == "POST"
        and re.fullmatch(
            r"/v1/jobs/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}/cancellation",
            path,
        )
        is not None
    ):
        return ControlPlaneScope.CANCEL
    return ControlPlaneScope.WRITE


def _bounded_headers(scope: Scope) -> dict[bytes, tuple[bytes, ...]] | None:
    raw_headers = scope.get("headers")
    if not isinstance(raw_headers, (list, tuple)):
        return None
    selected: dict[bytes, list[bytes]] = {
        b"authorization": [],
        b"x-project-id": [],
    }
    for item in raw_headers:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], bytes)
            or not isinstance(item[1], bytes)
        ):
            return None
        name, value = item
        lowered = name.lower()
        if lowered not in selected:
            continue
        selected[lowered].append(value)
    return {name: tuple(values) for name, values in selected.items()}


def _presented_token(values: tuple[bytes, ...]) -> tuple[str, bool]:
    if len(values) != 1:
        return _DUMMY_TOKEN, False
    if len(values[0]) > _MAX_AUTHORIZATION_HEADER_BYTES or not values[0].isascii():
        return _DUMMY_TOKEN, False
    authorization = values[0].decode("ascii")
    if not authorization.startswith("Bearer "):
        return _DUMMY_TOKEN, False
    token = authorization.removeprefix("Bearer ")
    return (token, True) if _canonical_token(token) else (_DUMMY_TOKEN, False)


def _presented_project(values: tuple[bytes, ...]) -> str | None:
    if len(values) != 1:
        return None
    if len(values[0]) > _MAX_PROJECT_HEADER_BYTES or not values[0].isascii():
        return None
    value = values[0].decode("ascii")
    if re.fullmatch(_IDENTIFIER_PATTERN, value) is None:
        return None
    return value


def _canonical_token(token: str) -> bool:
    return _TOKEN_PATTERN.fullmatch(token) is not None


def _token_digest_unchecked(token: str) -> str:
    return f"sha256:{sha256(token.encode('ascii')).hexdigest()}"


__all__ = [
    "AuthenticationConfiguration",
    "AuthorizationFailure",
    "AuthorizedPrincipal",
    "ControlPlaneAuthorizer",
    "ControlPlaneScope",
    "PrincipalConfiguration",
    "SecurityConfigurationError",
    "digest_token",
    "load_authentication_configuration",
    "principal_from_scope",
]
