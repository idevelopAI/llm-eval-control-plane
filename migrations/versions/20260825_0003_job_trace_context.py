"""Persist privacy-safe W3C trace coordination for durable jobs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_0003"
down_revision: str | None = "20260823_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("control_plane_jobs") as batch_op:
        batch_op.add_column(
            sa.Column("traceparent", sa.String(length=55), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_control_plane_jobs_traceparent_length",
            "traceparent IS NULL OR length(traceparent) = 55",
        )


def downgrade() -> None:
    with op.batch_alter_table("control_plane_jobs") as batch_op:
        batch_op.drop_constraint(
            "ck_control_plane_jobs_traceparent_length",
            type_="check",
        )
        batch_op.drop_column("traceparent")
