"""add run tokens and external results

Revision ID: db7a03a3b897
Revises: 285facc1b692
Create Date: 2026-11-04 02:16:50.465541
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "db7a03a3b897"
down_revision: str | Sequence[str] | None = "285facc1b692"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("attempts", sa.Column("run_token_hash", sa.Text(), nullable=True))
    op.add_column(
        "attempts", sa.Column("run_token_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "attempts",
        sa.Column("external_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("attempts", "external_result")
    op.drop_column("attempts", "run_token_expires_at")
    op.drop_column("attempts", "run_token_hash")
