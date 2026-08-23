"""Add durable worker payloads, leases, and attempt history."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260823_0002"
down_revision: str | None = "20260820_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_ERROR_CODE = "legacy_payload_missing"
_DOWNGRADE_ERROR_CODE = "worker_state_removed"


def upgrade() -> None:
    op.add_column(
        "control_plane_jobs",
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "control_plane_jobs",
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("3"),
            nullable=False,
        ),
    )
    op.add_column(
        "control_plane_jobs",
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
    )

    jobs = sa.table(
        "control_plane_jobs",
        sa.column("status", sa.String(length=16)),
        sa.column("error_code", sa.String(length=64)),
        sa.column("attempt_count", sa.Integer()),
        sa.column("available_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("version", sa.Integer()),
    )
    op.execute(
        jobs.update().values(
            attempt_count=1,
            available_at=jobs.c.created_at,
        )
    )
    op.execute(
        jobs.update()
        .where(jobs.c.status.in_(("queued", "running")))
        .values(
            status="failed",
            error_code=_LEGACY_ERROR_CODE,
            updated_at=sa.case(
                (
                    jobs.c.updated_at >= sa.func.current_timestamp(),
                    jobs.c.updated_at,
                ),
                else_=sa.func.current_timestamp(),
            ),
            version=jobs.c.version + 1,
        )
    )

    with op.batch_alter_table("control_plane_jobs") as batch_op:
        batch_op.drop_constraint(
            "ck_control_plane_jobs_status",
            type_="check",
        )
        batch_op.alter_column(
            "available_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_control_plane_jobs_status",
            "status IN ('queued', 'running', 'cancel_requested', "
            "'succeeded', 'failed', 'canceled')",
        )
        batch_op.create_check_constraint(
            "ck_control_plane_jobs_attempt_count",
            "attempt_count >= 0 AND attempt_count <= max_attempts",
        )
        batch_op.create_check_constraint(
            "ck_control_plane_jobs_max_attempts",
            "max_attempts BETWEEN 1 AND 10",
        )
        batch_op.create_check_constraint(
            "ck_control_plane_jobs_queued_attempt",
            "status <> 'queued' OR attempt_count < max_attempts",
        )
        batch_op.create_check_constraint(
            "ck_control_plane_jobs_started_attempt",
            "status NOT IN ('running', 'cancel_requested', 'succeeded', 'failed') "
            "OR attempt_count > 0",
        )
        batch_op.create_check_constraint(
            "ck_control_plane_jobs_available_at",
            "available_at >= created_at",
        )
        batch_op.create_check_constraint(
            "ck_control_plane_jobs_updated_at",
            "updated_at >= created_at",
        )

    op.create_index(
        "ix_control_plane_jobs_claimable",
        "control_plane_jobs",
        ("available_at", "created_at", "job_id"),
        unique=False,
        postgresql_where=sa.text("status = 'queued'"),
        sqlite_where=sa.text("status = 'queued'"),
    )

    op.create_table(
        "control_plane_job_payloads",
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("payload_digest", sa.String(length=71), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(payload_digest) = 71",
            name="ck_control_plane_job_payloads_digest_length",
        ),
        sa.ForeignKeyConstraint(
            ("job_id",),
            ("control_plane_jobs.job_id",),
            name="fk_control_plane_job_payloads_job",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("job_id", name="pk_control_plane_job_payloads"),
    )

    op.create_table(
        "control_plane_job_attempts",
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("lease_token", sa.String(length=128), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempt_number > 0",
            name="ck_control_plane_job_attempts_attempt_number",
        ),
        sa.CheckConstraint(
            "length(worker_id) BETWEEN 1 AND 128",
            name="ck_control_plane_job_attempts_worker_id_length",
        ),
        sa.CheckConstraint(
            "length(lease_token) BETWEEN 32 AND 128",
            name="ck_control_plane_job_attempts_lease_token_length",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'retry_scheduled', 'failed', "
            "'canceled', 'lease_expired')",
            name="ck_control_plane_job_attempts_status",
        ),
        sa.CheckConstraint(
            "heartbeat_at >= started_at",
            name="ck_control_plane_job_attempts_heartbeat_at",
        ),
        sa.CheckConstraint(
            "lease_expires_at > heartbeat_at",
            name="ck_control_plane_job_attempts_lease_expires_at",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= heartbeat_at",
            name="ck_control_plane_job_attempts_finished_at",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) OR "
            "(status <> 'running' AND finished_at IS NOT NULL)",
            name="ck_control_plane_job_attempts_terminal_time",
        ),
        sa.CheckConstraint(
            "(status IN ('retry_scheduled', 'failed', 'lease_expired') "
            "AND error_code IS NOT NULL) OR "
            "(status NOT IN ('retry_scheduled', 'failed', 'lease_expired') "
            "AND error_code IS NULL)",
            name="ck_control_plane_job_attempts_failure_code",
        ),
        sa.ForeignKeyConstraint(
            ("job_id",),
            ("control_plane_jobs.job_id",),
            name="fk_control_plane_job_attempts_job",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "job_id",
            "attempt_number",
            name="pk_control_plane_job_attempts",
        ),
        sa.UniqueConstraint(
            "job_id",
            "lease_token",
            name="uq_control_plane_job_attempts_job_lease_token",
        ),
    )
    op.create_index(
        "ux_control_plane_job_attempts_active_job",
        "control_plane_job_attempts",
        ("job_id",),
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
        sqlite_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "ix_control_plane_job_attempts_expiring",
        "control_plane_job_attempts",
        ("lease_expires_at", "job_id", "attempt_number"),
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
        sqlite_where=sa.text("status = 'running'"),
    )

    _record_legacy_attempts()


def downgrade() -> None:
    jobs = sa.table(
        "control_plane_jobs",
        sa.column("status", sa.String(length=16)),
        sa.column("error_code", sa.String(length=64)),
        sa.column("attempt_count", sa.Integer()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("version", sa.Integer()),
    )
    op.execute(
        jobs.update()
        .where(jobs.c.status.in_(("queued", "running", "cancel_requested", "canceled")))
        .values(
            status="failed",
            error_code=_DOWNGRADE_ERROR_CODE,
            attempt_count=sa.case(
                (jobs.c.attempt_count < 1, 1),
                else_=jobs.c.attempt_count,
            ),
            updated_at=sa.case(
                (
                    jobs.c.updated_at >= sa.func.current_timestamp(),
                    jobs.c.updated_at,
                ),
                else_=sa.func.current_timestamp(),
            ),
            version=jobs.c.version + 1,
        )
    )

    op.drop_index(
        "ix_control_plane_job_attempts_expiring",
        table_name="control_plane_job_attempts",
    )
    op.drop_index(
        "ux_control_plane_job_attempts_active_job",
        table_name="control_plane_job_attempts",
    )
    op.drop_table("control_plane_job_attempts")
    op.drop_table("control_plane_job_payloads")
    op.drop_index(
        "ix_control_plane_jobs_claimable",
        table_name="control_plane_jobs",
    )

    with op.batch_alter_table("control_plane_jobs") as batch_op:
        batch_op.drop_constraint(
            "ck_control_plane_jobs_updated_at",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_control_plane_jobs_available_at",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_control_plane_jobs_started_attempt",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_control_plane_jobs_queued_attempt",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_control_plane_jobs_max_attempts",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_control_plane_jobs_attempt_count",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_control_plane_jobs_status",
            type_="check",
        )
        batch_op.drop_column("available_at")
        batch_op.drop_column("max_attempts")
        batch_op.drop_column("attempt_count")
        batch_op.create_check_constraint(
            "ck_control_plane_jobs_status",
            "status IN ('queued', 'running', 'succeeded', 'failed')",
        )


def _record_legacy_attempts() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        lease_expires_at = "updated_at + INTERVAL '1 second'"
    elif dialect == "sqlite":
        lease_expires_at = "datetime(updated_at, '+1 second')"
    else:
        raise RuntimeError("control-plane migrations require PostgreSQL or SQLite")

    op.execute(
        sa.text(
            "INSERT INTO control_plane_job_attempts "
            "(job_id, attempt_number, status, worker_id, lease_token, error_code, "
            "started_at, heartbeat_at, lease_expires_at, finished_at) "
            "SELECT job_id, 1, status, 'phase5-migration', "
            "'phase5-migration-token-000000000000', error_code, "
            f"created_at, updated_at, {lease_expires_at}, updated_at "
            "FROM control_plane_jobs"
        )
    )
