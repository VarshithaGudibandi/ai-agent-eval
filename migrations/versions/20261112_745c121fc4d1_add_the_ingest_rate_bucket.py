"""add the ingest rate bucket

Revision ID: 745c121fc4d1
Revises: 76e9308e7b1f
Create Date: 2026-11-12 07:10:33.713858
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "745c121fc4d1"
down_revision: str | Sequence[str] | None = "76e9308e7b1f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("ingest_bucket", sa.Double(), nullable=True))
    op.add_column(
        "workspaces",
        sa.Column("ingest_bucket_refreshed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "ingest_bucket_refreshed_at")
    op.drop_column("workspaces", "ingest_bucket")
