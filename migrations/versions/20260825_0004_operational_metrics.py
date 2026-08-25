"""Persist bounded aggregate run usage for operational metrics."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pydantic import ValidationError
from sqlalchemy.engine import Connection, RowMapping

from llm_eval_control_plane.domain.canonical import (
    CanonicalJsonError,
    canonical_json_bytes,
    parse_json,
)
from llm_eval_control_plane.domain.results import RunResult

revision: str = "20260825_0004"
down_revision: str | None = "20260825_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
_MAX_OPERATIONAL_VALUE = 2**63 - 1

_runs = sa.table(
    "control_plane_runs",
    sa.column("run_id", sa.String(length=128)),
    sa.column("result_digest", sa.String(length=71)),
    sa.column("dataset_name", sa.String(length=128)),
    sa.column("dataset_revision", sa.Integer()),
    sa.column("status", sa.String(length=32)),
    sa.column("execution_mode", sa.String(length=32)),
    sa.column("input_units", sa.BigInteger()),
    sa.column("output_units", sa.BigInteger()),
    sa.column("document", sa.Text()),
)


def upgrade() -> None:
    connection = op.get_bind()
    _validate_existing_runs(connection)

    op.add_column(
        "control_plane_runs",
        sa.Column("input_units", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "control_plane_runs",
        sa.Column("output_units", sa.BigInteger(), nullable=True),
    )
    _backfill_runs(connection)

    with op.batch_alter_table("control_plane_runs") as batch_op:
        batch_op.alter_column(
            "input_units",
            existing_type=sa.BigInteger(),
            nullable=False,
        )
        batch_op.alter_column(
            "output_units",
            existing_type=sa.BigInteger(),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_control_plane_runs_input_units",
            "input_units >= 0",
        )
        batch_op.create_check_constraint(
            "ck_control_plane_runs_output_units",
            "output_units >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("control_plane_runs") as batch_op:
        batch_op.drop_constraint(
            "ck_control_plane_runs_output_units",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_control_plane_runs_input_units",
            type_="check",
        )
        batch_op.drop_column("output_units")
        batch_op.drop_column("input_units")


def _validate_existing_runs(connection: Connection) -> None:
    for row in connection.execute(_existing_runs()).mappings():
        _run_usage(row)


def _backfill_runs(connection: Connection) -> None:
    for row in connection.execute(_existing_runs()).mappings():
        input_units, output_units = _run_usage(row)
        connection.execute(
            _runs.update()
            .where(_runs.c.run_id == row["run_id"])
            .values(input_units=input_units, output_units=output_units)
        )


def _existing_runs() -> sa.Select[tuple[object, ...]]:
    return sa.select(
        _runs.c.run_id,
        _runs.c.result_digest,
        _runs.c.dataset_name,
        _runs.c.dataset_revision,
        _runs.c.status,
        _runs.c.execution_mode,
        _runs.c.document,
    )


def _run_usage(row: RowMapping) -> tuple[int, int]:
    try:
        document = row["document"]
        if not isinstance(document, str):
            raise TypeError("run document must be text")
        if len(document.encode("utf-8")) > _MAX_DOCUMENT_BYTES:
            raise ValueError("run document exceeds its size bound")
        value = parse_json(document)
        if canonical_json_bytes(value).decode("utf-8") != document:
            raise ValueError("run document is not canonical")
        result = RunResult.model_validate(value)
        if (
            row["run_id"],
            row["result_digest"],
            row["dataset_name"],
            row["dataset_revision"],
            row["status"],
            row["execution_mode"],
        ) != (
            result.run_id,
            result.result_digest,
            result.dataset.name,
            result.dataset.revision,
            result.status.value,
            result.execution_mode.value,
        ):
            raise ValueError("run indexes do not match evidence")
        input_units = sum(
            case.target.response.usage.input_units
            for case in result.cases
            if case.target is not None
        )
        output_units = sum(
            case.target.response.usage.output_units
            for case in result.cases
            if case.target is not None
        )
        if (
            input_units > _MAX_OPERATIONAL_VALUE
            or output_units > _MAX_OPERATIONAL_VALUE
        ):
            raise ValueError("run usage exceeds its supported range")
        return input_units, output_units
    except (
        CanonicalJsonError,
        KeyError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValidationError,
        ValueError,
    ):
        raise RuntimeError("Stored run evidence is invalid") from None
