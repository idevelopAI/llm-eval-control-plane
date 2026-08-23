"""Create durable control-plane metadata and evidence tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "control_plane_datasets",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(length=71), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(digest) = 71",
            name="ck_control_plane_datasets_digest_length",
        ),
        sa.CheckConstraint("revision > 0", name="ck_control_plane_datasets_revision"),
        sa.PrimaryKeyConstraint("name", "revision", name="pk_control_plane_datasets"),
    )
    op.create_index(
        "ix_control_plane_datasets_created_name_revision",
        "control_plane_datasets",
        ("created_at", "name", "revision"),
        unique=False,
    )
    op.create_index(
        "ix_control_plane_datasets_name_created_revision",
        "control_plane_datasets",
        ("name", "created_at", "revision"),
        unique=False,
    )
    op.create_index(
        "ix_control_plane_datasets_digest",
        "control_plane_datasets",
        ("digest",),
        unique=False,
    )

    op.create_table(
        "control_plane_jobs",
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=71), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND error_code IS NOT NULL) OR "
            "(status <> 'failed' AND error_code IS NULL)",
            name="ck_control_plane_jobs_failure_code",
        ),
        sa.CheckConstraint(
            "kind IN ('run', 'comparison')", name="ck_control_plane_jobs_kind"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_control_plane_jobs_status",
        ),
        sa.CheckConstraint("version >= 0", name="ck_control_plane_jobs_version"),
        sa.PrimaryKeyConstraint("job_id", name="pk_control_plane_jobs"),
        sa.UniqueConstraint(
            "kind",
            "idempotency_key",
            name="uq_control_plane_jobs_kind_idempotency_key",
        ),
        sa.UniqueConstraint(
            "kind",
            "resource_id",
            name="uq_control_plane_jobs_kind_resource_id",
        ),
    )
    op.create_index(
        "ix_control_plane_jobs_status_created_job_id",
        "control_plane_jobs",
        ("status", "created_at", "job_id"),
        unique=False,
    )
    op.create_index(
        "ix_control_plane_jobs_created_job_id",
        "control_plane_jobs",
        ("created_at", "job_id"),
        unique=False,
    )
    op.create_index(
        "ix_control_plane_jobs_kind_created_job_id",
        "control_plane_jobs",
        ("kind", "created_at", "job_id"),
        unique=False,
    )
    op.create_index(
        "ix_control_plane_jobs_kind_status_created_job_id",
        "control_plane_jobs",
        ("kind", "status", "created_at", "job_id"),
        unique=False,
    )

    op.create_table(
        "control_plane_runs",
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("result_digest", sa.String(length=71), nullable=False),
        sa.Column("dataset_name", sa.String(length=128), nullable=False),
        sa.Column("dataset_revision", sa.Integer(), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "dataset_revision > 0",
            name="ck_control_plane_runs_dataset_revision",
        ),
        sa.CheckConstraint(
            "length(result_digest) = 71",
            name="ck_control_plane_runs_result_digest_length",
        ),
        sa.ForeignKeyConstraint(
            ("dataset_name", "dataset_revision"),
            (
                "control_plane_datasets.name",
                "control_plane_datasets.revision",
            ),
            name="fk_control_plane_runs_dataset",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_control_plane_runs"),
    )
    op.create_index(
        "ix_control_plane_runs_dataset_created_run_id",
        "control_plane_runs",
        ("dataset_name", "created_at", "run_id"),
        unique=False,
    )
    op.create_index(
        "ix_control_plane_runs_created_run_id",
        "control_plane_runs",
        ("created_at", "run_id"),
        unique=False,
    )
    op.create_index(
        "ix_control_plane_runs_result_digest",
        "control_plane_runs",
        ("result_digest",),
        unique=False,
    )

    op.create_table(
        "control_plane_release_decisions",
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("decision_digest", sa.String(length=71), nullable=False),
        sa.Column("baseline_run_id", sa.String(length=128), nullable=False),
        sa.Column("candidate_run_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(decision_digest) = 71",
            name="ck_control_plane_release_decisions_digest_length",
        ),
        sa.CheckConstraint(
            "status IN ('passed', 'failed')",
            name="ck_control_plane_release_decisions_status",
        ),
        sa.ForeignKeyConstraint(
            ("baseline_run_id",),
            ("control_plane_runs.run_id",),
            name="fk_control_plane_release_decisions_baseline_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("candidate_run_id",),
            ("control_plane_runs.run_id",),
            name="fk_control_plane_release_decisions_candidate_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "decision_id", name="pk_control_plane_release_decisions"
        ),
    )
    op.create_index(
        "ix_control_plane_release_decisions_status_created_decision_id",
        "control_plane_release_decisions",
        ("status", "created_at", "decision_id"),
        unique=False,
    )
    op.create_index(
        "ix_control_plane_release_decisions_created_decision_id",
        "control_plane_release_decisions",
        ("created_at", "decision_id"),
        unique=False,
    )
    op.create_index(
        "ix_control_plane_release_decisions_decision_digest",
        "control_plane_release_decisions",
        ("decision_digest",),
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_control_plane_release_decisions_decision_digest",
        table_name="control_plane_release_decisions",
    )
    op.drop_index(
        "ix_control_plane_release_decisions_created_decision_id",
        table_name="control_plane_release_decisions",
    )
    op.drop_index(
        "ix_control_plane_release_decisions_status_created_decision_id",
        table_name="control_plane_release_decisions",
    )
    op.drop_table("control_plane_release_decisions")
    op.drop_index(
        "ix_control_plane_runs_result_digest",
        table_name="control_plane_runs",
    )
    op.drop_index(
        "ix_control_plane_runs_created_run_id",
        table_name="control_plane_runs",
    )
    op.drop_index(
        "ix_control_plane_runs_dataset_created_run_id",
        table_name="control_plane_runs",
    )
    op.drop_table("control_plane_runs")
    op.drop_index(
        "ix_control_plane_jobs_kind_status_created_job_id",
        table_name="control_plane_jobs",
    )
    op.drop_index(
        "ix_control_plane_jobs_kind_created_job_id",
        table_name="control_plane_jobs",
    )
    op.drop_index(
        "ix_control_plane_jobs_created_job_id",
        table_name="control_plane_jobs",
    )
    op.drop_index(
        "ix_control_plane_jobs_status_created_job_id",
        table_name="control_plane_jobs",
    )
    op.drop_table("control_plane_jobs")
    op.drop_index(
        "ix_control_plane_datasets_digest",
        table_name="control_plane_datasets",
    )
    op.drop_index(
        "ix_control_plane_datasets_name_created_revision",
        table_name="control_plane_datasets",
    )
    op.drop_index(
        "ix_control_plane_datasets_created_name_revision",
        table_name="control_plane_datasets",
    )
    op.drop_table("control_plane_datasets")
