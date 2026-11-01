"""add provider credentials

Revision ID: 6e3071241f4c
Revises: a8216f494a4d
Create Date: 2026-11-01 18:14:32.302196
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6e3071241f4c"
down_revision: str | Sequence[str] | None = "a8216f494a4d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_last4", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint("provider IN ('gemini')", name=op.f("ck_provider_credentials_provider")),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_provider_credentials_created_by_user_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_provider_credentials_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_credentials")),
        sa.UniqueConstraint(
            "workspace_id", "id", name=op.f("uq_provider_credentials_workspace_id_id")
        ),
    )


def downgrade() -> None:
    op.drop_table("provider_credentials")
