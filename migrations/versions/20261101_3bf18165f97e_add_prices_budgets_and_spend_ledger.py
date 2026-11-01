"""add prices budgets and spend ledger

Revision ID: 3bf18165f97e
Revises: 6e3071241f4c
Create Date: 2026-11-01 18:17:11.878598
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3bf18165f97e"
down_revision: str | Sequence[str] | None = "6e3071241f4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_audit_log_actor_type"), "audit_log", type_="check")
    op.create_check_constraint(
        op.f("ck_audit_log_actor_type"), "audit_log", "actor_type IN ('user', 'token', 'system')"
    )
    op.create_table(
        "price_versions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("input_per_million_micros", sa.BigInteger(), nullable=False),
        sa.Column("cached_input_per_million_micros", sa.BigInteger(), nullable=False),
        sa.Column("output_per_million_micros", sa.BigInteger(), nullable=False),
        sa.Column("other_per_million_micros", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "input_per_million_micros >= 0 AND cached_input_per_million_micros >= 0 AND output_per_million_micros >= 0 AND other_per_million_micros >= 0",
            name=op.f("ck_price_versions_prices_not_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_price_versions_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_versions")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_price_versions_workspace_id_id")),
    )
    op.create_table(
        "budgets",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("experiment_id", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("money_limit_micros", sa.BigInteger(), nullable=True),
        sa.Column("tokens_limit", sa.BigInteger(), nullable=True),
        sa.Column("requests_limit", sa.BigInteger(), nullable=True),
        sa.Column("money_used_micros", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("tokens_used", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("requests_used", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("warned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "money_used_micros >= 0 AND tokens_used >= 0 AND requests_used >= 0",
            name=op.f("ck_budgets_used_not_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name=op.f("fk_budgets_workspace_id_experiment_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_budgets_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budgets")),
        sa.UniqueConstraint("experiment_id", name=op.f("uq_budgets_experiment_id")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_budgets_workspace_id_id")),
    )
    op.create_table(
        "spend_ledger",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("budget_id", sa.Text(), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=True),
        sa.Column("reservation_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("money_micros", sa.BigInteger(), nullable=False),
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.Column("requests", sa.BigInteger(), nullable=False),
        sa.Column("cost_known", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('reservation', 'reconciliation')", name=op.f("ck_spend_ledger_kind")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "budget_id"],
            ["budgets.workspace_id", "budgets.id"],
            name=op.f("fk_spend_ledger_workspace_id_budget_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_spend_ledger_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spend_ledger")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_spend_ledger_workspace_id_id")),
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_audit_log_actor_type"), "audit_log", type_="check")
    op.create_check_constraint(
        op.f("ck_audit_log_actor_type"), "audit_log", "actor_type IN ('user', 'token')"
    )
    op.drop_table("spend_ledger")
    op.drop_table("budgets")
    op.drop_table("price_versions")
