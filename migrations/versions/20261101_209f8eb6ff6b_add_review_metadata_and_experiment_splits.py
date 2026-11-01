"""add review metadata and experiment splits

Revision ID: 209f8eb6ff6b
Revises: ecd164928825
Create Date: 2026-11-01 22:44:21.621450
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "209f8eb6ff6b"
down_revision: str | Sequence[str] | None = "ecd164928825"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rows written before this migration get these values once. New rows must set their own.
_BACKFILL = {
    ("experiments", "split"): (sa.Text(), sa.text("'dev'")),
    ("experiments", "final"): (sa.Boolean(), sa.false()),
    ("task_versions", "category"): (sa.Text(), sa.text("'uncategorized'")),
    ("task_versions", "difficulty"): (sa.Text(), sa.text("'medium'")),
    ("task_versions", "review"): (postgresql.JSONB(astext_type=sa.Text()), sa.text("'{}'")),
}


def upgrade() -> None:
    for (table, column), (column_type, default) in _BACKFILL.items():
        op.add_column(table, sa.Column(column, column_type, server_default=default, nullable=False))
        op.alter_column(table, column, server_default=None)
    op.add_column("task_versions", sa.Column("reviewer", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_experiments_split"),
        "experiments",
        "split IN ('dev', 'validation', 'held_out')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_experiments_split"), "experiments", type_="check")
    op.drop_column("task_versions", "reviewer")
    for table, column in reversed(_BACKFILL):
        op.drop_column(table, column)
