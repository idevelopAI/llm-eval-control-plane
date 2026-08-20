"""Atomic, append-only persistence for complete local run artifacts."""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import suppress
from hashlib import sha256
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from llm_eval_control_plane.domain.canonical import (
    CanonicalJsonError,
    canonical_json_bytes,
    parse_json,
)
from llm_eval_control_plane.domain.execution import RunId
from llm_eval_control_plane.domain.results import RunResult

_STORAGE_SCHEMA = "run/v1"
_RUN_ID_ADAPTER = TypeAdapter(RunId)
_DEFAULT_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


class RunStoreError(RuntimeError):
    """Base class for filesystem run-store failures with safe messages."""


class InvalidRunIdError(RunStoreError):
    """Raised before path construction when a run ID is invalid."""


class RunNotFoundError(RunStoreError):
    """Raised when a requested immutable run does not exist."""


class RunConflictError(RunStoreError):
    """Raised when a run ID already names different immutable content."""


class CorruptRunError(RunStoreError):
    """Raised when a stored run cannot be safely validated."""


def _validated_run_id(value: str) -> str:
    try:
        return _RUN_ID_ADAPTER.validate_python(value, strict=True)
    except ValidationError as error:
        raise InvalidRunIdError("Run ID is invalid") from error


def _serialized_result(result: RunResult) -> bytes:
    return (
        canonical_json_bytes(
            {
                "result": result.model_dump(
                    mode="json",
                    by_alias=False,
                    exclude_defaults=False,
                    exclude_none=False,
                    exclude_unset=False,
                ),
                "storage_schema": _STORAGE_SCHEMA,
            }
        )
        + b"\n"
    )


def _storage_filename(run_id: str) -> str:
    key = f"llm-eval-control-plane/run-id/v1\0{run_id}".encode()
    return f"{sha256(key).hexdigest()}.json"


class FilesystemRunRepository:
    """Store immutable run results below one owner-only local directory."""

    def __init__(
        self,
        root: Path,
        *,
        max_artifact_bytes: int = _DEFAULT_MAX_ARTIFACT_BYTES,
    ) -> None:
        if type(max_artifact_bytes) is not int or max_artifact_bytes <= 0:
            raise ValueError("Maximum artifact size must be a positive integer")
        self._runs_directory = root / "runs"
        self._max_artifact_bytes = max_artifact_bytes

    def save(self, result: RunResult) -> None:
        """Atomically publish a run once, allowing identical retries."""
        destination = self._path_for(result.run_id)
        payload = _serialized_result(result)
        if len(payload) > self._max_artifact_bytes:
            raise RunStoreError("Run artifact exceeds the configured size limit")
        self._ensure_directory()

        if destination.exists():
            self._require_identical(destination, payload, result.run_id)
            return

        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._runs_directory,
            prefix=".tmp-",
            suffix=".json",
        )
        temporary = Path(temporary_name)
        try:
            if os.name != "nt":  # pragma: no branch - platform-specific hardening
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())

            try:
                os.link(temporary, destination)
            except FileExistsError:
                self._require_identical(destination, payload, result.run_id)
        except RunStoreError:
            raise
        except OSError as error:
            raise RunStoreError("Could not store run artifact") from error
        finally:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        self._sync_directory()

    def get(self, run_id: str) -> RunResult:
        """Load and fully validate one stored run artifact."""
        safe_run_id = _validated_run_id(run_id)
        path = self._runs_directory / _storage_filename(safe_run_id)
        try:
            payload = self._read_regular_file(path)
        except FileNotFoundError as error:
            raise RunNotFoundError("Run artifact was not found") from error
        except OSError as error:
            raise CorruptRunError("Run artifact could not be read") from error

        return self._validated_payload(payload, safe_run_id)

    def _path_for(self, run_id: str) -> Path:
        safe_run_id = _validated_run_id(run_id)
        return self._runs_directory / _storage_filename(safe_run_id)

    def _ensure_directory(self) -> None:
        try:
            self._runs_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            descriptor = os.open(self._runs_directory, flags)
            try:
                if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise OSError("run store is not a directory")
                if os.name != "nt":  # pragma: no branch - POSIX permissions
                    os.fchmod(descriptor, 0o700)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise RunStoreError("Could not prepare run store") from error

    def _require_identical(
        self,
        path: Path,
        expected: bytes,
        expected_run_id: str,
    ) -> None:
        try:
            actual = self._read_regular_file(path)
        except OSError as error:
            raise CorruptRunError("Existing run artifact could not be read") from error
        if actual != expected:
            self._validated_payload(actual, expected_run_id)
            raise RunConflictError("Run ID already contains different content")

    @staticmethod
    def _validated_payload(payload: bytes, expected_run_id: str) -> RunResult:
        try:
            document = parse_json(payload.decode("utf-8"))
            if payload != canonical_json_bytes(document) + b"\n":
                raise ValueError("storage envelope is not canonical")
            if not isinstance(document, dict) or set(document) != {
                "result",
                "storage_schema",
            }:
                raise ValueError("invalid storage envelope")
            if document["storage_schema"] != _STORAGE_SCHEMA:
                raise ValueError("unsupported storage schema")
            result = RunResult.model_validate(document["result"])
            if result.run_id != expected_run_id:
                raise ValueError("run ID does not match its storage key")
        except (
            CanonicalJsonError,
            UnicodeDecodeError,
            ValidationError,
            TypeError,
            ValueError,
        ) as error:
            raise CorruptRunError("Run artifact failed integrity validation") from error
        return result

    def _read_regular_file(self, path: Path) -> bytes:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("run artifact is not a regular file")
            if metadata.st_size > self._max_artifact_bytes:
                raise OSError("run artifact exceeds the configured size limit")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                payload = stream.read(self._max_artifact_bytes + 1)
                if len(payload) > self._max_artifact_bytes:
                    raise OSError("run artifact exceeds the configured size limit")
                return payload
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _sync_directory(self) -> None:
        if os.name == "nt":  # pragma: no cover - Windows has no directory fsync
            return
        descriptor = os.open(self._runs_directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
