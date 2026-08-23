"""Production composition root for the local PostgreSQL API service."""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import create_engine

from llm_eval_control_plane.adapters.control_plane_db import (
    SqlAlchemyControlPlaneRepository,
)
from llm_eval_control_plane.api.app import create_app
from llm_eval_control_plane.api.execution import DeterministicEvaluationExecutor
from llm_eval_control_plane.api.settings import (
    database_url_from_environment,
    max_body_bytes_from_environment,
    worker_settings_from_environment,
)
from llm_eval_control_plane.application.control_plane import ControlPlaneService


def create_runtime_app() -> FastAPI:
    """Create the env-configured FastAPI app without mutating the schema."""
    engine = create_engine(
        database_url_from_environment(),
        pool_pre_ping=True,
        hide_parameters=True,
    )
    repository = SqlAlchemyControlPlaneRepository(engine)
    worker_settings = worker_settings_from_environment()
    service = ControlPlaneService(
        repository=repository,
        executor=DeterministicEvaluationExecutor(),
        max_attempts=worker_settings.max_attempts,
    )
    app = create_app(
        service=service,
        max_body_bytes=max_body_bytes_from_environment(),
    )
    app.state.control_plane_engine = engine
    app.router.add_event_handler("shutdown", engine.dispose)
    return app


__all__ = ["create_runtime_app"]
