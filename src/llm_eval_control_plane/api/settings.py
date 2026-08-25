"""Secret-safe runtime configuration for the PostgreSQL control plane."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
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
_MAX_HEALTH_FILE_BYTES = 4 * 1024

_WORKER_MAX_ATTEMPTS = "CONTROL_PLANE_WORKER_MAX_ATTEMPTS"
_WORKER_LEASE_SECONDS = "CONTROL_PLANE_WORKER_LEASE_SECONDS"
_WORKER_HEARTBEAT_SECONDS = "CONTROL_PLANE_WORKER_HEARTBEAT_SECONDS"
_WORKER_POLL_MILLISECONDS = "CONTROL_PLANE_WORKER_POLL_MILLISECONDS"
_WORKER_REAPER_BATCH = "CONTROL_PLANE_WORKER_REAPER_BATCH"
_WORKER_BACKOFF_BASE_SECONDS = "CONTROL_PLANE_WORKER_BACKOFF_BASE_SECONDS"
_WORKER_BACKOFF_MAX_SECONDS = "CONTROL_PLANE_WORKER_BACKOFF_MAX_SECONDS"
_WORKER_HEALTH_FILE = "CONTROL_PLANE_WORKER_HEALTH_FILE"


class RuntimeConfigurationError(RuntimeError):
    """A content-safe runtime configuration failure."""


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """Bounded, non-secret settings shared by workers and the lease reaper."""

    max_attempts: int = 3
    lease_seconds: int = 30
    heartbeat_seconds: int = 10
    poll_milliseconds: int = 500
    reaper_batch: int = 50
    backoff_base_seconds: int = 1
    backoff_max_seconds: int = 60
    health_file: Path = Path("/tmp/control-plane-worker.ready")

    def __post_init__(self) -> None:
        bounds = (
            (self.max_attempts, 1, 10),
            (self.lease_seconds, 5, 3_600),
            (self.heartbeat_seconds, 1, 1_800),
            (self.poll_milliseconds, 50, 60_000),
            (self.reaper_batch, 1, 100),
            (self.backoff_base_seconds, 1, 300),
            (self.backoff_max_seconds, 1, 3_600),
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not lower <= value <= upper
            for value, lower, upper in bounds
        ):
            raise RuntimeConfigurationError("Worker configuration is invalid")
        if self.heartbeat_seconds * 2 > self.lease_seconds:
            raise RuntimeConfigurationError("Worker configuration is invalid")
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise RuntimeConfigurationError("Worker configuration is invalid")
        if not _valid_health_file(self.health_file):
            raise RuntimeConfigurationError("Worker configuration is invalid")


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


def worker_settings_from_environment(
    environ: Mapping[str, str] = os.environ,
) -> WorkerSettings:
    """Resolve bounded worker controls without reading or retaining secrets."""
    return WorkerSettings(
        max_attempts=_bounded_worker_decimal(
            environ.get(_WORKER_MAX_ATTEMPTS),
            default=3,
            lower=1,
            upper=10,
        ),
        lease_seconds=_bounded_worker_decimal(
            environ.get(_WORKER_LEASE_SECONDS),
            default=30,
            lower=5,
            upper=3_600,
        ),
        heartbeat_seconds=_bounded_worker_decimal(
            environ.get(_WORKER_HEARTBEAT_SECONDS),
            default=10,
            lower=1,
            upper=1_800,
        ),
        poll_milliseconds=_bounded_worker_decimal(
            environ.get(_WORKER_POLL_MILLISECONDS),
            default=500,
            lower=50,
            upper=60_000,
        ),
        reaper_batch=_bounded_worker_decimal(
            environ.get(_WORKER_REAPER_BATCH),
            default=50,
            lower=1,
            upper=100,
        ),
        backoff_base_seconds=_bounded_worker_decimal(
            environ.get(_WORKER_BACKOFF_BASE_SECONDS),
            default=1,
            lower=1,
            upper=300,
        ),
        backoff_max_seconds=_bounded_worker_decimal(
            environ.get(_WORKER_BACKOFF_MAX_SECONDS),
            default=60,
            lower=1,
            upper=3_600,
        ),
        health_file=_worker_health_file(environ.get(_WORKER_HEALTH_FILE)),
    )


def _bounded_worker_decimal(
    raw: object | None,
    *,
    default: int,
    lower: int,
    upper: int,
) -> int:
    if raw is None:
        return default
    if not isinstance(raw, str) or not raw or not raw.isascii() or not raw.isdecimal():
        raise RuntimeConfigurationError("Worker configuration is invalid")
    value = int(raw)
    if not lower <= value <= upper:
        raise RuntimeConfigurationError("Worker configuration is invalid")
    return value


def _worker_health_file(raw: object | None) -> Path:
    if raw is None:
        return Path("/tmp/control-plane-worker.ready")
    if not isinstance(raw, str):
        raise RuntimeConfigurationError("Worker configuration is invalid")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError:
        raise RuntimeConfigurationError("Worker configuration is invalid") from None
    path = Path(raw)
    if (
        not raw
        or len(encoded) > _MAX_HEALTH_FILE_BYTES
        or any(character in raw for character in ("\x00", "\r", "\n"))
        or not path.is_absolute()
        or not path.name
        or raw != str(path)
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise RuntimeConfigurationError("Worker configuration is invalid")
    return path


def _valid_health_file(path: object) -> bool:
    if not isinstance(path, Path):
        return False
    raw = str(path)
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (
        bool(raw)
        and len(encoded) <= _MAX_HEALTH_FILE_BYTES
        and not any(character in raw for character in ("\x00", "\r", "\n"))
        and path.is_absolute()
        and bool(path.name)
        and raw == str(path)
        and not any(part in {".", ".."} for part in path.parts)
    )


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
    if url.query:
        raise RuntimeConfigurationError("Database URL options are unsupported")
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
    "WorkerSettings",
    "database_url_from_environment",
    "max_body_bytes_from_environment",
    "worker_settings_from_environment",
]
