"""Index existing suite pins without rewriting canonical evidence.

Pause API writers and workers before this maintenance migration. The backfill
and index creation can lock populated evidence tables. No old unpinned evidence
is assigned a suite, even if a matching suite now exists in the registry.
"""

import sqlalchemy as sa
from alembic import op

revision = "20260915_0006"
down_revision = "20260903_0005"
branch_labels = None
depends_on = None


def _constraint(prefixes: tuple[str, ...]) -> str:
    absent = " AND ".join(
        f"{prefix}_{field} IS NULL"
        for prefix in prefixes
        for field in ("name", "revision", "digest")
    )
    present = " AND ".join(
        f"{prefix}_name IS NOT NULL AND length({prefix}_name) BETWEEN 1 AND 128 "
        f"AND {prefix}_revision IS NOT NULL AND {prefix}_revision > 0 "
        f"AND {prefix}_digest IS NOT NULL AND length({prefix}_digest) = 71"
        for prefix in prefixes
    )
    return f"({absent}) OR ({present})"


def upgrade() -> None:
    dialect = op.get_context().dialect.name
    if dialect not in ("postgresql", "sqlite"):
        raise RuntimeError("Suite history requires PostgreSQL or SQLite")
    for table, prefixes, identity in (
        ("control_plane_runs", ("suite", "target"), "run_id"),
        ("control_plane_release_decisions", ("suite",), "decision_id"),
    ):
        for prefix in prefixes:
            op.add_column(table, sa.Column(f"{prefix}_name", sa.String(128)))
            op.add_column(table, sa.Column(f"{prefix}_revision", sa.Integer()))
            op.add_column(table, sa.Column(f"{prefix}_digest", sa.String(71)))
        assignments: dict[str, sa.ColumnElement[object]] = {}
        for prefix in prefixes:
            for field in ("name", "revision", "digest"):
                if dialect == "postgresql":
                    value = f"CAST(document AS jsonb)->'{prefix}'->>'{field}'"
                    if field == "revision":
                        value = f"CAST({value} AS INTEGER)"
                else:
                    value = f"json_extract(document, '$.{prefix}.{field}')"
                assignments[f"{prefix}_{field}"] = sa.literal_column(value)
        if dialect == "postgresql":
            pinned = "CAST(document AS jsonb)->'suite' IS NOT NULL AND "
            pinned += "CAST(document AS jsonb)->'suite' <> 'null'::jsonb"
        else:
            pinned = "json_type(document, '$.suite') IS NOT NULL AND "
            pinned += "json_type(document, '$.suite') <> 'null'"
        # All SQL expressions above are fixed migration constants, never input.
        projection = sa.table(table, *(sa.column(name) for name in assignments))
        op.execute(sa.update(projection).values(**assignments).where(sa.text(pinned)))
        with op.batch_alter_table(table) as batch:
            batch.create_check_constraint(
                f"ck_{table}_suite_history", _constraint(prefixes)
            )
        op.create_index(
            f"ix_{table}_suite_history",
            table,
            ["suite_name", "suite_revision", "suite_digest", "created_at", identity],
        )


def downgrade() -> None:
    for table, prefixes in (
        ("control_plane_release_decisions", ("suite",)),
        ("control_plane_runs", ("suite", "target")),
    ):
        op.drop_index(f"ix_{table}_suite_history", table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"ck_{table}_suite_history", type_="check")
            for prefix in prefixes:
                for field in ("name", "revision", "digest"):
                    batch.drop_column(f"{prefix}_{field}")
