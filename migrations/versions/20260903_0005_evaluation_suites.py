"""Persist immutable versioned evaluation suites."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260903_0005"
down_revision: str | None = "20260825_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "control_plane_suites",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(length=71), nullable=False),
        sa.Column("dataset_name", sa.String(length=128), nullable=False),
        sa.Column("dataset_revision", sa.Integer(), nullable=False),
        sa.Column("evaluator_count", sa.Integer(), nullable=False),
        sa.Column("metric_count", sa.Integer(), nullable=False),
        sa.Column("slice_count", sa.Integer(), nullable=False),
        sa.Column("gate_count", sa.Integer(), nullable=False),
        sa.Column("execution_mode", sa.String(length=32), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "dataset_revision > 0",
            name="ck_control_plane_suites_dataset_revision",
        ),
        sa.CheckConstraint(
            "length(digest) = 71",
            name="ck_control_plane_suites_digest_length",
        ),
        sa.CheckConstraint(
            "evaluator_count BETWEEN 1 AND 32",
            name="ck_control_plane_suites_evaluator_count",
        ),
        sa.CheckConstraint(
            "gate_count BETWEEN 1 AND 64",
            name="ck_control_plane_suites_gate_count",
        ),
        sa.CheckConstraint(
            "execution_mode IN "
            "('offline_deterministic_fixture', 'offline_mock', 'live')",
            name="ck_control_plane_suites_execution_mode",
        ),
        sa.CheckConstraint(
            "metric_count BETWEEN 1 AND 32",
            name="ck_control_plane_suites_metric_count",
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_control_plane_suites_revision",
        ),
        sa.CheckConstraint(
            "slice_count BETWEEN 0 AND 128",
            name="ck_control_plane_suites_slice_count",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_name", "dataset_revision"],
            [
                "control_plane_datasets.name",
                "control_plane_datasets.revision",
            ],
            name="fk_control_plane_suites_dataset",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "name",
            "revision",
            name="pk_control_plane_suites",
        ),
    )
    op.create_index(
        "ix_control_plane_suites_created_name_revision",
        "control_plane_suites",
        ["created_at", "name", "revision"],
        unique=False,
    )
    op.create_index(
        "ix_control_plane_suites_dataset_name_revision",
        "control_plane_suites",
        ["dataset_name", "dataset_revision"],
        unique=False,
    )
    op.create_index(
        "ix_control_plane_suites_digest",
        "control_plane_suites",
        ["digest"],
        unique=False,
    )
    op.create_index(
        "ix_control_plane_suites_name_created_revision",
        "control_plane_suites",
        ["name", "created_at", "revision"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_control_plane_suites_name_created_revision",
        table_name="control_plane_suites",
    )
    op.drop_index(
        "ix_control_plane_suites_digest",
        table_name="control_plane_suites",
    )
    op.drop_index(
        "ix_control_plane_suites_dataset_name_revision",
        table_name="control_plane_suites",
    )
    op.drop_index(
        "ix_control_plane_suites_created_name_revision",
        table_name="control_plane_suites",
    )
    op.drop_table("control_plane_suites")
