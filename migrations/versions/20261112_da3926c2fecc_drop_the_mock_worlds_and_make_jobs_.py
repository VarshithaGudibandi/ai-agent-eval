"""drop the mock worlds and make jobs generic

Revision ID: da3926c2fecc
Revises: f208c2865614
Create Date: 2026-11-12 06:37:30.694518
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "da3926c2fecc"
down_revision: str | Sequence[str] | None = "f208c2865614"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Children before parents, so no foreign key is left pointing at a dropped table.
    op.drop_constraint(
        op.f("fk_model_calls_workspace_id_trace_event_id"), "model_calls", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("fk_model_calls_workspace_id_attempt_id"), "model_calls", type_="foreignkey"
    )
    op.drop_column("model_calls", "trace_event_id")
    op.drop_column("model_calls", "attempt_id")
    op.drop_column("model_calls", "self_reported")

    # Every queued job pointed at a v1 run, so none of them can survive the change.
    op.execute("DELETE FROM jobs")
    op.drop_constraint(op.f("fk_jobs_workspace_id_run_id"), "jobs", type_="foreignkey")
    op.drop_constraint(op.f("fk_jobs_workspace_id_experiment_id"), "jobs", type_="foreignkey")
    op.drop_constraint(op.f("uq_jobs_run_id"), "jobs", type_="unique")
    op.drop_column("jobs", "run_id")
    op.drop_column("jobs", "experiment_id")
    op.drop_column("jobs", "position")
    op.add_column("jobs", sa.Column("kind", sa.Text(), nullable=False))
    op.add_column("jobs", sa.Column("subject_id", sa.Text(), nullable=False))
    op.add_column(
        "jobs",
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column("jobs", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column("jobs", sa.Column("lease_token", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("worker_id", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint(op.f("ck_jobs_status"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_status"),
        "jobs",
        "status IN ('queued', 'running', 'done', 'failed', 'cancelled')",
    )
    op.create_check_constraint(op.f("ck_jobs_attempts"), "jobs", "attempts >= 0")
    op.create_index(
        "uq_jobs_pending_work",
        "jobs",
        ["workspace_id", "kind", "subject_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.drop_column("spend_ledger", "attempt_id")
    op.add_column("spend_ledger", sa.Column("job_id", sa.Text(), nullable=True))

    for table in ("tool_calls", "trace_events", "env_snapshots", "env_records", "scores"):
        op.drop_table(table)
    op.drop_index(op.f("ix_attempts_lease"), table_name="attempts")
    op.drop_table("attempts")
    op.drop_index(op.f("ix_comparisons_workspace_created"), table_name="comparisons")
    op.drop_table("comparisons")
    op.drop_table("runs")
    op.drop_table("task_versions")
    op.drop_table("agent_configs")

    for column in (
        "signing_secret_nonce",
        "signing_secret_ciphertext",
        "signing_secret_last4",
        "signing_secret_created_at",
    ):
        op.drop_column("workspaces", column)


def downgrade() -> None:
    raise NotImplementedError("the v1 mock-world tables and their data cannot be restored")
