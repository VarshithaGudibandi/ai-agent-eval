"""add workspace signing secrets

Revision ID: fdb4dbd3c301
Revises: db7a03a3b897
Create Date: 2026-11-04 02:21:25.273407
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fdb4dbd3c301"
down_revision: str | Sequence[str] | None = "db7a03a3b897"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("signing_secret_nonce", sa.LargeBinary(), nullable=True))
    op.add_column(
        "workspaces", sa.Column("signing_secret_ciphertext", sa.LargeBinary(), nullable=True)
    )
    op.add_column("workspaces", sa.Column("signing_secret_last4", sa.Text(), nullable=True))
    op.add_column(
        "workspaces",
        sa.Column("signing_secret_created_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "signing_secret_created_at")
    op.drop_column("workspaces", "signing_secret_last4")
    op.drop_column("workspaces", "signing_secret_ciphertext")
    op.drop_column("workspaces", "signing_secret_nonce")
