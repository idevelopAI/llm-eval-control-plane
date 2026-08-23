"""Secret-safe runtime configuration for the PostgreSQL control plane."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

_DIRECT_URL = "CONTROL_PLANE_DATABASE_URL"
_HOST = "CONTROL_PLANE_DATABASE_HOST"
_PORT = "CONTROL_PLANE_DATABASE_PORT"
_NAME = "CONTROL_PLANE_DATABASE_NAME"
_USER = "CONTROL_PLANE_DATABASE_USER"
_PASSWORD_FILE = "CONTROL_PLANE_DATABASE_PASSWORD_FILE"
_COMPONENT_KEYS = (_HOST, _PORT, _NAME, _USER, _PASSWORD_FILE)
_MAX_SECRET_BYTES = 4 * 1024
_MAX_URL_CHARS = 4 * 1024
_DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024
_MAX_BODY_BYTES = 16 * 1024 * 1024


class RuntimeConfigurationError(RuntimeError):
    """A content-safe runtime configuration failure."""


def database_url_from_environment(
    environ: Mapping[str, str] = os.environ,
    *,
    fallback_url: str | None = None,
    allow_sqlite: bool = False,
) -> URL:
    """Resolve a PostgreSQL URL without logging or returning secret text."""
    direct = environ.get(_DIRECT_URL)
    configured_components = [key for key in _COMPONENT_KEYS if key in environ]
    if direct is not None and configured_components:
        raise RuntimeConfigurationError("Database configuration is ambiguous")
    if direct is not None:
        return _validated_url(direct, allow_sqlite=allow_sqlite)
    if configured_components:
        if len(configured_components) != len(_COMPONENT_KEYS):
            raise RuntimeConfigurationError("Database configuration is incomplete")
        return _component_url(environ)
    if fallback_url:
        return _validated_url(fallback_url, allow_sqlite=allow_sqlite)
    raise RuntimeConfigurationError("Database configuration is required")


def max_body_bytes_from_environment(
    environ: Mapping[str, str] = os.environ,
) -> int:
    """Resolve the bounded HTTP request-body limit."""
    raw = environ.get("CONTROL_PLANE_MAX_BODY_BYTES")
    if raw is None:
        return _DEFAULT_MAX_BODY_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeConfigurationError("Request body limit is invalid") from None
    if not 1 <= value <= _MAX_BODY_BYTES:
        raise RuntimeConfigurationError("Request body limit is invalid")
    return value


def _validated_url(value: str, *, allow_sqlite: bool) -> URL:
    if not value or len(value) > _MAX_URL_CHARS:
        raise RuntimeConfigurationError("Database URL is invalid")
    try:
        url = make_url(value)
    except (ArgumentError, ValueError):
        raise RuntimeConfigurationError("Database URL is invalid") from None
    allowed_drivers = {"postgresql+psycopg"}
    if allow_sqlite:
        allowed_drivers.add("sqlite+pysqlite")
    if url.drivername not in allowed_drivers:
        raise RuntimeConfigurationError("Database driver is unsupported")
    if not url.database:
        raise RuntimeConfigurationError("Database URL is incomplete")
    return url


def _component_url(environ: Mapping[str, str]) -> URL:
    host = environ[_HOST]
    name = environ[_NAME]
    user = environ[_USER]
    if not _safe_host(host) or not _safe_identifier(name) or not _safe_identifier(user):
        raise RuntimeConfigurationError("Database configuration is invalid")
    try:
        port = int(environ[_PORT])
    except ValueError:
        raise RuntimeConfigurationError("Database configuration is invalid") from None
    if not 1 <= port <= 65_535:
        raise RuntimeConfigurationError("Database configuration is invalid")
    password = _read_password(Path(environ[_PASSWORD_FILE]))
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host=host,
        port=port,
        database=name,
    )


def _safe_host(value: str) -> bool:
    return (
        1 <= len(value) <= 253
        and value.isascii()
        and value[0].isalnum()
        and all(character.isalnum() or character in ".-" for character in value)
    )


def _safe_identifier(value: str) -> bool:
    return (
        1 <= len(value) <= 63
        and value.isascii()
        and (value[0].isalpha() or value[0] == "_")
        and all(character.isalnum() or character in "_-" for character in value)
    )


def _read_password(path: Path) -> str:
    if not path.is_absolute():
        raise RuntimeConfigurationError("Database secret file is invalid")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_SECRET_BYTES:
            raise OSError("secret file is not a bounded regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(_MAX_SECRET_BYTES + 1)
    except OSError:
        raise RuntimeConfigurationError("Database secret file is invalid") from None
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
    if len(payload) > _MAX_SECRET_BYTES:
        raise RuntimeConfigurationError("Database secret file is invalid")
    payload = payload.rstrip(b"\r\n")
    try:
        password = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeConfigurationError("Database secret file is invalid") from None
    if (
        not password
        or len(password) > 1_024
        or any(character in password for character in ("\x00", "\r", "\n"))
    ):
        raise RuntimeConfigurationError("Database secret file is invalid")
    return password


__all__ = [
    "RuntimeConfigurationError",
    "database_url_from_environment",
    "max_body_bytes_from_environment",
]
