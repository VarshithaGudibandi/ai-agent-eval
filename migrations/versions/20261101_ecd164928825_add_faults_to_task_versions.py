"""add faults to task versions

Revision ID: ecd164928825
Revises: 17cb325c520b
Create Date: 2026-11-01 22:31:01.840894
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ecd164928825"
down_revision: str | Sequence[str] | None = "17cb325c520b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_versions",
        sa.Column(
            "faults",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("task_versions", "faults")
