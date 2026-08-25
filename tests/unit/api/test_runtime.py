from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from pytest import MonkeyPatch, raises

from llm_eval_control_plane.api import runtime
from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor


class DisposableEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class DisposableTelemetry:
    tracer = object()

    def __init__(self) -> None:
        self.stopped = False

    def shutdown(self) -> None:
        self.stopped = True


def test_runtime_factory_wires_secret_safe_postgres_dependencies(
    monkeypatch: MonkeyPatch,
) -> None:
    engine = DisposableEngine()
    snapshot = object()

    class RuntimeRepository:
        def operational_snapshot(self) -> object:
            return snapshot

    repository = RuntimeRepository()
    executor = object()
    service = object()
    authorizer = object()
    telemetry = DisposableTelemetry()
    captured: dict[str, Any] = {}

    def fake_create_engine(
        url: object,
        *,
        pool_pre_ping: bool,
        hide_parameters: bool,
    ) -> DisposableEngine:
        captured["engine"] = (url, pool_pre_ping, hide_parameters)
        return engine

    def fake_repository(value: object) -> object:
        captured["repository_engine"] = value
        return repository

    def fake_service(
        *,
        repository: object,
        executor: object,
        max_attempts: int,
    ) -> object:
        captured["service"] = (repository, executor, max_attempts)
        return service

    def fake_create_app(
        *,
        service: object,
        authorizer: object,
        telemetry: object,
        max_body_bytes: int,
    ) -> FastAPI:
        captured["app"] = (service, authorizer, telemetry, max_body_bytes)
        return FastAPI()

    def fake_observability(
        *,
        service: str,
        log_sink: object,
        operational_snapshot_provider: Callable[[], object],
    ) -> DisposableTelemetry:
        captured["telemetry"] = (
            service,
            callable(log_sink),
            operational_snapshot_provider(),
        )
        return telemetry

    monkeypatch.setattr(runtime, "database_url_from_environment", lambda: "safe-url")
    monkeypatch.setattr(
        runtime,
        "authentication_file_from_environment",
        lambda: "safe-auth-file",
    )
    monkeypatch.setattr(runtime, "max_body_bytes_from_environment", lambda: 4_096)
    monkeypatch.setattr(
        runtime,
        "worker_settings_from_environment",
        lambda: SimpleNamespace(max_attempts=5),
    )
    monkeypatch.setattr(runtime, "create_engine", fake_create_engine)
    monkeypatch.setattr(runtime, "SqlAlchemyControlPlaneRepository", fake_repository)
    monkeypatch.setattr(
        runtime,
        "ControlPlaneAuthorizer",
        SimpleNamespace(from_file=lambda path: authorizer),
    )
    monkeypatch.setattr(runtime, "Observability", fake_observability)
    monkeypatch.setattr(
        runtime,
        "DeterministicEvaluationExecutor",
        lambda *, tracer: executor if tracer is telemetry.tracer else None,
    )
    monkeypatch.setattr(runtime, "ControlPlaneService", fake_service)
    monkeypatch.setattr(runtime, "create_app", fake_create_app)

    app = runtime.create_runtime_app()

    assert captured == {
        "engine": ("safe-url", True, True),
        "repository_engine": engine,
        "telemetry": ("api", True, snapshot),
        "service": (repository, executor, 5),
        "app": (service, authorizer, telemetry, 4_096),
    }
    assert app.state.control_plane_engine is engine
    shutdown_handlers = tuple(app.router.on_shutdown)
    assert len(shutdown_handlers) == 1
    handler = shutdown_handlers[0]
    assert callable(handler)
    handler()
    assert engine.disposed is True
    assert telemetry.stopped is True


def test_deterministic_executor_rejects_unknown_evaluator_names() -> None:
    with raises(ValueError, match="unsupported evaluator"):
        DeterministicEvaluationExecutor().validate(
            target_name="fake/target",
            target_revision=1,
            adapter="deterministic_fake",
            evaluator_names=("private-evaluator",),
            scenario_overrides={},
        )
