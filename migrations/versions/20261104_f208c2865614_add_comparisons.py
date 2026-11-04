"""add comparisons

Revision ID: f208c2865614
Revises: fdb4dbd3c301
Create Date: 2026-11-04 07:43:49.431050
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f208c2865614"
down_revision: str | Sequence[str] | None = "fdb4dbd3c301"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "comparisons",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("baseline_experiment_id", sa.Text(), nullable=False),
        sa.Column("baseline_config_id", sa.Text(), nullable=False),
        sa.Column("candidate_experiment_id", sa.Text(), nullable=False),
        sa.Column("candidate_config_id", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "baseline_config_id"],
            ["agent_configs.workspace_id", "agent_configs.id"],
            name=op.f("fk_comparisons_workspace_id_baseline_config_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "baseline_experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name=op.f("fk_comparisons_workspace_id_baseline_experiment_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "candidate_config_id"],
            ["agent_configs.workspace_id", "agent_configs.id"],
            name=op.f("fk_comparisons_workspace_id_candidate_config_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "candidate_experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name=op.f("fk_comparisons_workspace_id_candidate_experiment_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_comparisons_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_comparisons")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_comparisons_workspace_id_id")),
    )
    op.create_index(
        "ix_comparisons_workspace_created",
        "comparisons",
        ["workspace_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_comparisons_workspace_created", table_name="comparisons")
    op.drop_table("comparisons")
