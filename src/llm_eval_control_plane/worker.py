"""Production process for leased evaluation and comparison workers."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import signal
import socket
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from types import FrameType
from typing import Protocol, cast

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from llm_eval_control_plane.adapters.control_plane_db import (
    SqlAlchemyControlPlaneRepository,
)
from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.api.settings import (
    RuntimeConfigurationError,
    WorkerSettings,
    database_url_from_environment,
    worker_settings_from_environment,
)
from llm_eval_control_plane.application.control_plane import (
    ControlPlaneRepository,
    ControlPlaneStoreError,
)
from llm_eval_control_plane.application.worker import (
    WorkerError,
    WorkerResult,
    WorkerResultStatus,
    WorkerService,
    WorkerUnavailableError,
)
from llm_eval_control_plane.observability import Observability

_WORKER_COMPONENT = re.compile(r"[^A-Za-z0-9._:-]+")


class AttemptRunner(Protocol):
    async def run_once(self) -> WorkerResult: ...


class WorkerRuntime:
    """Poll durable work, reap expired attempts, and manage readiness state."""

    __slots__ = ("_repository", "_runner", "_settings", "_telemetry")

    def __init__(
        self,
        *,
        repository: ControlPlaneRepository,
        runner: AttemptRunner,
        settings: WorkerSettings,
        telemetry: Observability | None = None,
    ) -> None:
        self._repository = repository
        self._runner = runner
        self._settings = settings
        self._telemetry = telemetry

    def __repr__(self) -> str:
        return "WorkerRuntime()"

    async def serve(self, stop: asyncio.Event) -> None:
        """Run until stopped, draining an active attempt before returning."""
        ready = False
        persistence_unavailable = False
        self._observe(lambda telemetry: telemetry.emit_worker_lifecycle("started"))
        try:
            while not stop.is_set():
                try:
                    self._repository.check_health()
                    if not self._repository.schema_is_current():
                        raise ControlPlaneStoreError(
                            "Control-plane schema is unavailable"
                        )
                    recovered = self._repository.reap_expired_jobs(
                        limit=self._settings.reaper_batch,
                        retry_base_seconds=self._settings.backoff_base_seconds,
                        retry_max_seconds=self._settings.backoff_max_seconds,
                    )
                    self._record_worker_recovery(len(recovered))
                    if persistence_unavailable:
                        self._observe(
                            lambda telemetry: telemetry.emit_worker_lifecycle(
                                "persistence_recovered"
                            )
                        )
                        persistence_unavailable = False
                    _write_health_file(self._settings.health_file)
                    if not ready:
                        self._observe(
                            lambda telemetry: telemetry.set_worker_ready(True)
                        )
                        ready = True
                    result = await self._runner.run_once()
                except (ControlPlaneStoreError, WorkerUnavailableError):
                    _remove_health_file(self._settings.health_file)
                    if ready:
                        self._observe(
                            lambda telemetry: telemetry.set_worker_ready(False)
                        )
                        ready = False
                    if not persistence_unavailable:
                        self._observe(
                            lambda telemetry: telemetry.emit_worker_lifecycle(
                                "persistence_unavailable"
                            )
                        )
                        persistence_unavailable = True
                    self._observe(
                        lambda telemetry: telemetry.record_worker_poll("unavailable")
                    )
                    await self._wait_for_poll(stop)
                    continue
                if result.status is WorkerResultStatus.IDLE:
                    self._observe(
                        lambda telemetry: telemetry.record_worker_poll("idle")
                    )
                    await self._wait_for_poll(stop)
                else:
                    self._observe(
                        lambda telemetry: telemetry.record_worker_poll("processed")
                    )
                    self._record_worker_job_result(str(result.status))
        finally:
            _remove_health_file(self._settings.health_file)
            if ready:
                self._observe(lambda telemetry: telemetry.set_worker_ready(False))
            self._observe(lambda telemetry: telemetry.emit_worker_lifecycle("stopped"))

    def _observe(self, callback: Callable[[Observability], object]) -> None:
        telemetry = self._telemetry
        if telemetry is None:
            return
        try:
            callback(telemetry)
        except Exception:
            return

    def _record_worker_recovery(self, recovered_jobs: int) -> None:
        self._observe(
            lambda telemetry: telemetry.record_worker_recovery(recovered_jobs)
        )

    def _record_worker_job_result(self, result: str) -> None:
        self._observe(lambda telemetry: telemetry.record_worker_job_result(result))

    async def _wait_for_poll(self, stop: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=self._settings.poll_milliseconds / 1_000,
            )
        except TimeoutError:
            return


def _worker_id() -> str:
    """Return a bounded unique private identity without exposing it in output."""
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = "host"
    component = _WORKER_COMPONENT.sub("-", hostname).strip("-._:")[:64] or "host"
    return f"worker-{component}-{secrets.token_hex(12)}"


def _write_health_file(path: Path) -> None:
    """Atomically publish a content-free readiness marker."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = -1
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        if os.write(descriptor, b"ready\n") != len(b"ready\n"):
            raise OSError("worker readiness write was incomplete")
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
    except OSError as error:
        raise WorkerError("Worker readiness is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _remove_health_file(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows fallback
            signal.signal(signum, request_stop)


async def _run() -> None:
    settings = worker_settings_from_environment()
    engine: Engine | None = None
    telemetry: Observability | None = None
    try:
        telemetry = Observability(service="worker", log_sink=_stdout_log_sink)
        engine = create_engine(
            database_url_from_environment(),
            pool_pre_ping=True,
            hide_parameters=True,
        )
        repository = SqlAlchemyControlPlaneRepository(engine)
        worker_repository = cast(ControlPlaneRepository, repository)
        worker = WorkerService(
            repository=worker_repository,
            executor=DeterministicEvaluationExecutor(tracer=telemetry.tracer),
            worker_id=_worker_id(),
            lease_seconds=settings.lease_seconds,
            heartbeat_seconds=settings.heartbeat_seconds,
            backoff_base_seconds=settings.backoff_base_seconds,
            backoff_max_seconds=settings.backoff_max_seconds,
            trace_job=telemetry.trace_job,
        )
        runtime = WorkerRuntime(
            repository=worker_repository,
            runner=worker,
            settings=settings,
            telemetry=telemetry,
        )
        stop = asyncio.Event()
        _install_signal_handlers(stop)
        await runtime.serve(stop)
    finally:
        _remove_health_file(settings.health_file)
        if engine is not None:
            engine.dispose()
        if telemetry is not None:
            telemetry.shutdown()


def _stdout_log_sink(document: str) -> None:
    """Write one already-sanitized event line to the process log stream."""
    sys.stdout.write(document)
    sys.stdout.flush()


def main() -> int:
    """Run the worker with content-safe process failures."""
    try:
        asyncio.run(_run())
    except (RuntimeConfigurationError, WorkerError, ControlPlaneStoreError):
        print("Control-plane worker could not continue", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["WorkerRuntime", "main"]
