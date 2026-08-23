"""Alembic environment for the control-plane metadata database."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import URL

from llm_eval_control_plane.adapters.control_plane_db import (
    CONTROL_PLANE_METADATA,
)
from llm_eval_control_plane.api.settings import database_url_from_environment

config = context.config
if config.config_file_name is not None and config.get_section("loggers") is not None:
    fileConfig(config.config_file_name)

target_metadata = CONTROL_PLANE_METADATA


def _database_url() -> URL:
    return database_url_from_environment(
        os.environ,
        fallback_url=config.get_main_option("sqlalchemy.url") or None,
        allow_sqlite=True,
    )


def run_migrations_offline() -> None:
    """Generate SQL without opening a database connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations through a short-lived, non-pooled engine."""
    connectable = create_engine(
        _database_url(),
        poolclass=pool.NullPool,
        hide_parameters=True,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
