"""tie experiments and runs to versions and cases

Revision ID: 37aff41ef075
Revises: 3e5fe21dfe4b
Create Date: 2026-11-12 15:10:15.130325
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "37aff41ef075"
down_revision: str | Sequence[str] | None = "3e5fe21dfe4b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Experiments from before agent versions existed cannot be placed on one, so they go,
    # with their budgets. There is no production data.
    op.execute(
        "DELETE FROM spend_ledger WHERE budget_id IN "
        "(SELECT id FROM budgets WHERE experiment_id IS NOT NULL)"
    )
    op.execute("DELETE FROM budgets WHERE experiment_id IS NOT NULL")
    op.execute("DELETE FROM experiments")
    op.drop_constraint(op.f("ck_experiments_status"), "experiments", type_="check")
    op.create_check_constraint(
        op.f("ck_experiments_status"),
        "experiments",
        "status IN ('running', 'finished', 'paused_budget')",
    )
    op.add_column("experiments", sa.Column("agent_version_id", sa.Text(), nullable=False))
    op.add_column(
        "experiments", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_experiments_workspace_id_agent_version_id"),
        "experiments",
        "agent_versions",
        ["workspace_id", "agent_version_id"],
        ["workspace_id", "id"],
    )
    op.drop_column("experiments", "split")
    op.drop_column("experiments", "final")
    op.drop_column("experiments", "manifest")
    op.drop_column("experiments", "mode")
    op.add_column("runs", sa.Column("experiment_id", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("case_id", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("repetition", sa.Integer(), nullable=True))
    op.create_check_constraint(op.f("ck_runs_repetition"), "runs", "repetition >= 1")
    op.create_unique_constraint(
        op.f("uq_runs_experiment_id_case_id_repetition"),
        "runs",
        ["experiment_id", "case_id", "repetition"],
    )
    op.create_foreign_key(
        op.f("fk_runs_workspace_id_case_id"),
        "runs",
        "dataset_cases",
        ["workspace_id", "case_id"],
        ["workspace_id", "id"],
    )
    op.create_foreign_key(
        op.f("fk_runs_workspace_id_experiment_id"),
        "runs",
        "experiments",
        ["workspace_id", "experiment_id"],
        ["workspace_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_runs_repetition"), "runs", type_="check")
    op.drop_constraint(op.f("fk_runs_workspace_id_experiment_id"), "runs", type_="foreignkey")
    op.drop_constraint(op.f("fk_runs_workspace_id_case_id"), "runs", type_="foreignkey")
    op.drop_constraint(op.f("uq_runs_experiment_id_case_id_repetition"), "runs", type_="unique")
    op.drop_column("runs", "repetition")
    op.drop_column("runs", "case_id")
    op.drop_column("runs", "experiment_id")
    op.execute(
        "DELETE FROM spend_ledger WHERE budget_id IN "
        "(SELECT id FROM budgets WHERE experiment_id IS NOT NULL)"
    )
    op.execute("DELETE FROM budgets WHERE experiment_id IS NOT NULL")
    op.execute("DELETE FROM experiments")
    op.drop_constraint(op.f("ck_experiments_status"), "experiments", type_="check")
    op.create_check_constraint(
        op.f("ck_experiments_status"),
        "experiments",
        "status IN ('queued', 'running', 'paused_budget', 'paused_user', 'completed', "
        "'closed_partial', 'cancelled')",
    )
    op.add_column("experiments", sa.Column("mode", sa.TEXT(), autoincrement=False, nullable=False))
    op.add_column(
        "experiments",
        sa.Column(
            "manifest", postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False
        ),
    )
    op.add_column(
        "experiments", sa.Column("final", sa.BOOLEAN(), autoincrement=False, nullable=False)
    )
    op.add_column("experiments", sa.Column("split", sa.TEXT(), autoincrement=False, nullable=False))
    op.drop_constraint(
        op.f("fk_experiments_workspace_id_agent_version_id"), "experiments", type_="foreignkey"
    )
    op.drop_column("experiments", "finished_at")
    op.drop_column("experiments", "agent_version_id")
