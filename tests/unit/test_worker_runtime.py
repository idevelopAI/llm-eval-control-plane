from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Never, cast

from pytest import CaptureFixture, MonkeyPatch, raises

from llm_eval_control_plane.api.settings import WorkerSettings
from llm_eval_control_plane.application.control_plane import (
    ControlPlaneRepository,
    ControlPlaneStoreError,
)
from llm_eval_control_plane.application.worker import (
    WorkerError,
    WorkerResult,
    WorkerResultStatus,
)
from llm_eval_control_plane.worker import (
    WorkerRuntime,
    _remove_health_file,
    _worker_id,
    _write_health_file,
    main,
)


class RuntimeRepository:
    def __init__(self) -> None:
        self.healthy = True
        self.current = True
        self.reaped = 0

    def check_health(self) -> None:
        if not self.healthy:
            raise ControlPlaneStoreError("private-database-password")

    def schema_is_current(self) -> bool:
        return self.current

    def reap_expired_jobs(
        self,
        *,
        limit: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
    ) -> tuple[object, ...]:
        assert (limit, retry_base_seconds, retry_max_seconds) == (7, 2, 20)
        self.reaped += 1
        return ()


class RuntimeRunner:
    def __init__(self, outcomes: Iterator[WorkerResult]) -> None:
        self._outcomes = outcomes
        self.calls = 0

    async def run_once(self) -> WorkerResult:
        self.calls += 1
        return next(self._outcomes)


def _settings(path: Path) -> WorkerSettings:
    return WorkerSettings(
        poll_milliseconds=50,
        reaper_batch=7,
        backoff_base_seconds=2,
        backoff_max_seconds=20,
        health_file=path,
    )


def test_runtime_publishes_readiness_and_stops_without_another_claim(
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[int, int, bool]:
        stop = asyncio.Event()
        path = tmp_path / "worker.ready"
        repository = RuntimeRepository()

        class StoppingRunner:
            calls = 0

            async def run_once(self) -> WorkerResult:
                self.calls += 1
                assert path.read_text() == "ready\n" if path.exists() else True
                stop.set()
                return WorkerResult(status=WorkerResultStatus.SUCCEEDED)

        runner = StoppingRunner()
        runtime = WorkerRuntime(
            repository=cast(ControlPlaneRepository, repository),
            runner=runner,
            settings=_settings(path),
        )
        await runtime.serve(stop)
        return repository.reaped, runner.calls, path.exists()

    assert asyncio.run(exercise()) == (1, 1, False)


def test_idle_poll_can_be_stopped_and_removes_readiness(tmp_path: Path) -> None:
    async def exercise() -> tuple[int, bool]:
        path = tmp_path / "worker.ready"
        stop = asyncio.Event()
        runner = RuntimeRunner(iter((WorkerResult(status=WorkerResultStatus.IDLE),)))
        runtime = WorkerRuntime(
            repository=cast(ControlPlaneRepository, RuntimeRepository()),
            runner=runner,
            settings=_settings(path),
        )
        task = asyncio.create_task(runtime.serve(stop))
        while not path.exists():
            await asyncio.sleep(0)
        stop.set()
        await task
        return runner.calls, path.exists()

    assert asyncio.run(exercise()) == (1, False)


def test_unavailable_storage_withholds_readiness_until_stopped(
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[int, bool]:
        path = tmp_path / "worker.ready"
        path.write_text("stale\n")
        stop = asyncio.Event()
        repository = RuntimeRepository()
        repository.healthy = False
        runner = RuntimeRunner(iter(()))
        runtime = WorkerRuntime(
            repository=cast(ControlPlaneRepository, repository),
            runner=runner,
            settings=_settings(path),
        )
        task = asyncio.create_task(runtime.serve(stop))
        while path.exists():
            await asyncio.sleep(0)
        stop.set()
        await task
        return runner.calls, path.exists()

    assert asyncio.run(exercise()) == (0, False)


def test_health_marker_is_atomic_content_free_and_cleanup_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worker.ready"

    _write_health_file(path)
    assert path.read_bytes() == b"ready\n"
    assert not tuple(tmp_path.glob(".*.tmp"))

    _write_health_file(path)
    assert path.read_bytes() == b"ready\n"
    _remove_health_file(path)
    _remove_health_file(path)
    assert not path.exists()


def test_health_marker_failures_are_safe(tmp_path: Path) -> None:
    sentinel = "private-readiness-path"
    missing_parent = tmp_path / sentinel / "worker.ready"

    with raises(WorkerError) as captured:
        _write_health_file(missing_parent)

    assert sentinel not in str(captured.value)


def test_worker_identity_is_bounded_unique_and_not_in_runtime_repr(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "llm_eval_control_plane.worker.socket.gethostname",
        lambda: "private host/value" + "x" * 200,
    )

    first = _worker_id()
    second = _worker_id()

    assert first != second
    assert len(first) <= 96
    assert " " not in first
    assert "/" not in first
    runtime = WorkerRuntime(
        repository=cast(ControlPlaneRepository, RuntimeRepository()),
        runner=RuntimeRunner(iter(())),
        settings=WorkerSettings(),
    )
    assert repr(runtime) == "WorkerRuntime()"
    assert "private" not in repr(runtime)


def test_main_returns_safe_failure_without_exception_details(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    def fail() -> Never:
        raise WorkerError("private-api-key-value")

    monkeypatch.setattr("llm_eval_control_plane.worker._run", fail)

    assert main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Control-plane worker could not continue\n"
    assert "private-api-key-value" not in captured.err
