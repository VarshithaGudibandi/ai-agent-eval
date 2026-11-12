"""add dataset cases

Revision ID: 3e5fe21dfe4b
Revises: 745c121fc4d1
Create Date: 2026-11-12 15:06:50.135169
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3e5fe21dfe4b"
down_revision: str | Sequence[str] | None = "745c121fc4d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dataset_cases",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("dataset_version_id", sa.Text(), nullable=False),
        sa.Column("case_key", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expected", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "dataset_version_id"],
            ["dataset_versions.workspace_id", "dataset_versions.id"],
            name=op.f("fk_dataset_cases_workspace_id_dataset_version_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_dataset_cases_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_cases")),
        sa.UniqueConstraint(
            "dataset_version_id",
            "case_key",
            name=op.f("uq_dataset_cases_dataset_version_id_case_key"),
        ),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_dataset_cases_workspace_id_id")),
    )


def downgrade() -> None:
    op.drop_table("dataset_cases")
