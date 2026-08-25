from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Never, cast

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode
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
from llm_eval_control_plane.domain.control_plane import JobKind
from llm_eval_control_plane.observability import Observability
from llm_eval_control_plane.worker import (
    WorkerRuntime,
    _remove_health_file,
    _run,
    _worker_id,
    _write_health_file,
    main,
)

NOW = datetime(2026, 8, 25, 12, 34, 56, 789_000, tzinfo=UTC)
TRACE_ID = "1234567890abcdef1234567890abcdef"
PARENT_SPAN_ID = "1234567890abcdef"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01"


class StepClock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        self.value += 5_000_000
        return self.value


class RuntimeRepository:
    def __init__(self) -> None:
        self.healthy = True
        self.current = True
        self.reaped = 0
        self.recovered: tuple[object, ...] = ()

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
        return self.recovered


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


def _telemetry() -> tuple[
    Observability,
    list[str],
    InMemorySpanExporter,
    TracerProvider,
]:
    lines: list[str] = []
    exporter = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource(
            {
                "service.name": "llm-eval-control-plane-worker-test",
                "service.version": "test",
            }
        )
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = Observability(
        service="worker",
        version="test",
        tracer_provider=provider,
        log_sink=lines.append,
        clock_ns=StepClock(),
        wall_clock=lambda: NOW,
    )
    return telemetry, lines, exporter, provider


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


def test_runtime_exports_bounded_worker_metrics_and_transition_events(
    tmp_path: Path,
) -> None:
    telemetry, lines, _exporter, provider = _telemetry()

    async def exercise() -> tuple[int, int]:
        stop = asyncio.Event()
        repository = RuntimeRepository()
        repository.recovered = ("private-recovered-job-id",)

        class StoppingRunner:
            calls = 0

            async def run_once(self) -> WorkerResult:
                self.calls += 1
                stop.set()
                return WorkerResult(status=WorkerResultStatus.SUCCEEDED)

        runner = StoppingRunner()
        runtime = WorkerRuntime(
            repository=cast(ControlPlaneRepository, repository),
            runner=runner,
            settings=_settings(tmp_path / "worker.ready"),
            telemetry=telemetry,
        )
        await runtime.serve(stop)
        return repository.reaped, runner.calls

    assert asyncio.run(exercise()) == (1, 1)
    metrics = telemetry.render_metrics().body.decode("utf-8")
    assert 'control_plane_worker_polls_total{outcome="processed"} 1.0' in metrics
    assert 'control_plane_worker_job_results_total{result="succeeded"} 1.0' in metrics
    assert 'control_plane_worker_reaper_runs_total{outcome="recovered"} 1.0' in metrics
    assert "control_plane_worker_reaper_recovered_jobs_total 1.0" in metrics
    assert "control_plane_worker_ready 0.0" in metrics

    documents = [json.loads(line) for line in lines]
    assert [
        document["state"]
        for document in documents
        if document["event"] == "worker.lifecycle"
    ] == ["started", "ready", "not_ready", "stopped"]
    recovery = next(
        document
        for document in documents
        if document["event"] == "worker.recovery.completed"
    )
    assert recovery == {
        "event": "worker.recovery.completed",
        "recovered_jobs": 1,
        "schema_version": "control-plane-log/v1",
        "service": "worker",
        "severity": "WARNING",
        "timestamp": "2026-08-25T12:34:56.789Z",
    }
    rendered = "".join(lines) + metrics
    assert "private-recovered-job-id" not in rendered
    provider.shutdown()


def test_persistence_events_are_emitted_only_on_state_transitions(
    tmp_path: Path,
) -> None:
    telemetry, lines, _exporter, provider = _telemetry()

    async def exercise() -> int:
        stop = asyncio.Event()

        class RecoveringRepository(RuntimeRepository):
            checks = 0

            def check_health(self) -> None:
                self.checks += 1
                if self.checks <= 2:
                    raise ControlPlaneStoreError("private-database-password")

        class StoppingRunner:
            async def run_once(self) -> WorkerResult:
                stop.set()
                return WorkerResult(status=WorkerResultStatus.SUCCEEDED)

        repository = RecoveringRepository()
        runtime = WorkerRuntime(
            repository=cast(ControlPlaneRepository, repository),
            runner=StoppingRunner(),
            settings=_settings(tmp_path / "worker.ready"),
            telemetry=telemetry,
        )
        await runtime.serve(stop)
        return repository.checks

    assert asyncio.run(exercise()) == 3
    states = [
        document["state"]
        for document in map(json.loads, lines)
        if document["event"] == "worker.lifecycle"
    ]
    assert states.count("persistence_unavailable") == 1
    assert states.count("persistence_recovered") == 1
    assert "private-database-password" not in "".join(lines)
    metrics = telemetry.render_metrics().body.decode("utf-8")
    assert 'control_plane_worker_polls_total{outcome="unavailable"} 2.0' in metrics
    provider.shutdown()


def test_idle_polls_emit_metrics_without_log_spam(tmp_path: Path) -> None:
    telemetry, lines, _exporter, provider = _telemetry()

    async def exercise() -> int:
        stop = asyncio.Event()

        class IdleThenStopRunner:
            calls = 0

            async def run_once(self) -> WorkerResult:
                self.calls += 1
                if self.calls == 1:
                    return WorkerResult(status=WorkerResultStatus.IDLE)
                stop.set()
                return WorkerResult(status=WorkerResultStatus.SUCCEEDED)

        runner = IdleThenStopRunner()
        runtime = WorkerRuntime(
            repository=cast(ControlPlaneRepository, RuntimeRepository()),
            runner=runner,
            settings=_settings(tmp_path / "worker.ready"),
            telemetry=telemetry,
        )
        await runtime.serve(stop)
        return runner.calls

    assert asyncio.run(exercise()) == 2
    metrics = telemetry.render_metrics().body.decode("utf-8")
    assert 'control_plane_worker_polls_total{outcome="idle"} 1.0' in metrics
    assert all(json.loads(line)["event"] == "worker.lifecycle" for line in lines)
    provider.shutdown()


def test_claimed_job_span_is_linked_content_free_and_privacy_safe() -> None:
    telemetry, lines, exporter, provider = _telemetry()

    with (
        telemetry.trace_job(JobKind.RUN, TRACEPARENT),
        telemetry.tracer.start_as_current_span(
            "worker.child",
            record_exception=False,
            set_status_on_exception=False,
        ),
    ):
        pass

    spans: tuple[ReadableSpan, ...] = tuple(exporter.get_finished_spans())
    assert len(spans) == 2
    span = next(item for item in spans if item.name == "worker.job")
    child = next(item for item in spans if item.name == "worker.child")
    assert span.name == "worker.job"
    assert span.kind is SpanKind.CONSUMER
    assert span.parent is None
    assert span.attributes == {}
    assert span.events == ()
    assert span.status.status_code is StatusCode.UNSET
    assert len(span.links) == 1
    assert f"{span.links[0].context.trace_id:032x}" == TRACE_ID
    assert f"{span.links[0].context.span_id:016x}" == PARENT_SPAN_ID
    assert child.parent is not None
    assert child.parent.span_id == span.context.span_id

    document = json.loads(lines[-1])
    assert set(document) == {
        "duration_ms",
        "event",
        "job_kind",
        "outcome",
        "schema_version",
        "service",
        "severity",
        "span_id",
        "timestamp",
        "trace_id",
    }
    assert document["event"] == "worker.job.completed"
    assert document["job_kind"] == "run"
    assert document["outcome"] == "completed"
    metrics = telemetry.render_metrics().body.decode("utf-8")
    assert (
        'control_plane_worker_job_duration_seconds_count{kind="run",'
        'outcome="completed"} 1.0'
    ) in metrics
    assert TRACEPARENT not in "".join(lines) + metrics
    provider.shutdown()


def test_default_span_export_is_linked_and_drops_private_span_data() -> None:
    lines: list[str] = []
    telemetry = Observability(
        service="worker",
        version="test",
        log_sink=lines.append,
        wall_clock=lambda: NOW,
    )
    sentinel = "private-span-attribute-sentinel"

    with (
        telemetry.trace_job(JobKind.RUN, TRACEPARENT),
        telemetry.tracer.start_as_current_span(
            "evaluation.run",
            attributes={"private.attribute": sentinel},
            record_exception=False,
            set_status_on_exception=False,
        ) as span,
    ):
        span.add_event(sentinel)

    telemetry.shutdown()

    documents = tuple(json.loads(line) for line in lines)
    trace_documents = tuple(
        document
        for document in documents
        if document["event"] == "trace.span.completed"
    )
    assert {document["operation"] for document in trace_documents} == {
        "evaluation.run",
        "worker.job",
    }
    worker_span = next(
        document
        for document in trace_documents
        if document["operation"] == "worker.job"
    )
    assert worker_span["span_kind"] == "consumer"
    assert worker_span["linked_trace_id"] == TRACE_ID
    assert worker_span["linked_span_id"] == PARENT_SPAN_ID
    evaluation_span = next(
        document
        for document in trace_documents
        if document["operation"] == "evaluation.run"
    )
    assert evaluation_span["parent_span_id"] == worker_span["span_id"]
    rendered = "".join(lines)
    assert sentinel not in rendered
    assert "private.attribute" not in rendered
    assert TRACEPARENT not in rendered


def test_invalid_trace_context_and_telemetry_failures_do_not_affect_work(
    tmp_path: Path,
) -> None:
    private_trace = "private-trace-context-sentinel"
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def broken_sink(_document: str) -> Never:
        raise RuntimeError("private-log-sink-sentinel")

    telemetry = Observability(
        service="worker",
        tracer_provider=provider,
        log_sink=broken_sink,
    )
    with (
        raises(RuntimeError, match="work-failure"),
        telemetry.trace_job(JobKind.COMPARISON, private_trace),
    ):
        raise RuntimeError("work-failure")
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].links == ()
    assert spans[0].events == ()
    assert spans[0].status.status_code is StatusCode.UNSET

    class BrokenTelemetry:
        def __getattribute__(self, _name: str) -> Never:
            raise RuntimeError("private-telemetry-failure")

    async def exercise() -> int:
        stop = asyncio.Event()

        class StoppingRunner:
            calls = 0

            async def run_once(self) -> WorkerResult:
                self.calls += 1
                stop.set()
                return WorkerResult(status=WorkerResultStatus.SUCCEEDED)

        runner = StoppingRunner()
        runtime = WorkerRuntime(
            repository=cast(ControlPlaneRepository, RuntimeRepository()),
            runner=runner,
            settings=_settings(tmp_path / "worker.ready"),
            telemetry=cast(Observability, BrokenTelemetry()),
        )
        await runtime.serve(stop)
        return runner.calls

    assert asyncio.run(exercise()) == 1
    provider.shutdown()


def test_production_composition_shares_worker_tracer_and_shuts_down(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeTelemetry:
        tracer = object()
        shutdown_called = False

        def __init__(self, *, service: str, log_sink: object) -> None:
            captured["telemetry_service"] = service
            captured["log_sink"] = log_sink

        def trace_job(self, _kind: object, _traceparent: str | None) -> object:
            raise AssertionError(
                "composition should inject this method without calling it"
            )

        def shutdown(self) -> None:
            self.shutdown_called = True

    class FakeEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    class FakeRuntime:
        def __init__(self, **arguments: object) -> None:
            captured["runtime_arguments"] = arguments

        async def serve(self, stop: asyncio.Event) -> None:
            stop.set()

    telemetry = FakeTelemetry(service="worker", log_sink=object())
    engine = FakeEngine()
    executor = object()
    runner = object()
    repository = object()
    monkeypatch.setattr(
        "llm_eval_control_plane.worker.worker_settings_from_environment",
        lambda: _settings(tmp_path / "worker.ready"),
    )
    monkeypatch.setattr(
        "llm_eval_control_plane.worker.database_url_from_environment", object
    )

    def build_telemetry(**arguments: object) -> FakeTelemetry:
        captured["telemetry_arguments"] = arguments
        return telemetry

    monkeypatch.setattr("llm_eval_control_plane.worker.Observability", build_telemetry)
    monkeypatch.setattr(
        "llm_eval_control_plane.worker.create_engine",
        lambda *_arguments, **_keywords: engine,
    )
    monkeypatch.setattr(
        "llm_eval_control_plane.worker.SqlAlchemyControlPlaneRepository",
        lambda _engine: repository,
    )

    def build_executor(*, tracer: object) -> object:
        captured["executor_tracer"] = tracer
        return executor

    def build_worker(**arguments: object) -> object:
        captured["worker_arguments"] = arguments
        return runner

    monkeypatch.setattr(
        "llm_eval_control_plane.worker.DeterministicEvaluationExecutor",
        build_executor,
    )
    monkeypatch.setattr("llm_eval_control_plane.worker.WorkerService", build_worker)
    monkeypatch.setattr("llm_eval_control_plane.worker.WorkerRuntime", FakeRuntime)
    monkeypatch.setattr(
        "llm_eval_control_plane.worker._install_signal_handlers",
        lambda _stop: None,
    )

    asyncio.run(_run())

    worker_arguments = cast(dict[str, object], captured["worker_arguments"])
    runtime_arguments = cast(dict[str, object], captured["runtime_arguments"])
    telemetry_arguments = cast(dict[str, object], captured["telemetry_arguments"])
    assert telemetry_arguments["service"] == "worker"
    assert callable(telemetry_arguments["log_sink"])
    assert captured["executor_tracer"] is telemetry.tracer
    assert worker_arguments["executor"] is executor
    assert worker_arguments["trace_job"] == telemetry.trace_job
    assert runtime_arguments["runner"] is runner
    assert runtime_arguments["telemetry"] is telemetry
    assert engine.disposed is True
    assert telemetry.shutdown_called is True


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
