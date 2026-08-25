"""Production composition root for the local PostgreSQL API service."""

from __future__ import annotations

import sys

from fastapi import FastAPI
from sqlalchemy import create_engine

from llm_eval_control_plane.adapters.control_plane_db import (
    SqlAlchemyControlPlaneRepository,
)
from llm_eval_control_plane.api.app import create_app
from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.api.security import ControlPlaneAuthorizer
from llm_eval_control_plane.api.settings import (
    authentication_file_from_environment,
    database_url_from_environment,
    max_body_bytes_from_environment,
    worker_settings_from_environment,
)
from llm_eval_control_plane.application.control_plane import ControlPlaneService
from llm_eval_control_plane.observability import Observability


def create_runtime_app() -> FastAPI:
    """Create the env-configured FastAPI app without mutating the schema."""
    authorizer = ControlPlaneAuthorizer.from_file(
        authentication_file_from_environment()
    )
    telemetry = Observability(service="api", log_sink=_stdout_log_sink)
    engine = create_engine(
        database_url_from_environment(),
        pool_pre_ping=True,
        hide_parameters=True,
    )
    repository = SqlAlchemyControlPlaneRepository(engine)
    worker_settings = worker_settings_from_environment()
    service = ControlPlaneService(
        repository=repository,
        executor=DeterministicEvaluationExecutor(tracer=telemetry.tracer),
        max_attempts=worker_settings.max_attempts,
    )
    app = create_app(
        service=service,
        authorizer=authorizer,
        telemetry=telemetry,
        max_body_bytes=max_body_bytes_from_environment(),
    )
    app.state.control_plane_engine = engine
    app.state.control_plane_observability = telemetry

    def shutdown() -> None:
        engine.dispose()
        telemetry.shutdown()

    app.router.add_event_handler("shutdown", shutdown)
    return app


def _stdout_log_sink(document: str) -> None:
    """Write one already-sanitized event line to the process log stream."""
    sys.stdout.write(document)
    sys.stdout.flush()


__all__ = ["create_runtime_app"]
