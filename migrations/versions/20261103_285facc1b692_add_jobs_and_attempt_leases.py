"""add jobs and attempt leases

Revision ID: 285facc1b692
Revises: 209f8eb6ff6b
Create Date: 2026-11-03 18:57:07.692230
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "285facc1b692"
down_revision: str | Sequence[str] | None = "209f8eb6ff6b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("experiment_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'cancelled')", name=op.f("ck_jobs_status")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name=op.f("fk_jobs_workspace_id_experiment_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            name=op.f("fk_jobs_workspace_id_run_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_jobs_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("run_id", name=op.f("uq_jobs_run_id")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_jobs_workspace_id_id")),
    )
    op.create_index("ix_jobs_claimable", "jobs", ["status", "available_at"], unique=False)
    op.add_column("attempts", sa.Column("lease_token", sa.Text(), nullable=True))
    op.add_column(
        "attempts", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("attempts", sa.Column("worker_id", sa.Text(), nullable=True))
    op.create_index(
        "ix_attempts_lease", "attempts", ["execution_status", "lease_expires_at"], unique=False
    )
    # Runs created before the queue existed get a job, so a worker picks up the queued ones.
    op.execute(
        """
        INSERT INTO jobs (id, workspace_id, experiment_id, run_id, status, position)
        SELECT 'job_' || md5(r.id), r.workspace_id, r.experiment_id, r.id,
               CASE WHEN r.execution_status = 'queued' THEN 'queued' ELSE 'done' END,
               row_number() OVER (
                   PARTITION BY r.experiment_id
                   ORDER BY r.repetition, r.task_version_id, r.agent_config_id
               )
        FROM runs r
        """
    )


def downgrade() -> None:
    op.drop_index("ix_attempts_lease", table_name="attempts")
    op.drop_column("attempts", "worker_id")
    op.drop_column("attempts", "lease_expires_at")
    op.drop_column("attempts", "lease_token")
    op.drop_index("ix_jobs_claimable", table_name="jobs")
    op.drop_table("jobs")
