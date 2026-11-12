"""baseline

Revision ID: 880b4102ccce
Revises:
Create Date: 2026-11-12 06:47:41.882539
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "880b4102ccce"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
    )
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("token_prefix", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "role IN ('owner', 'member', 'viewer')", name=op.f("ck_api_tokens_role")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], name=op.f("fk_api_tokens_created_by_user_id")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_api_tokens_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_tokens_token_hash")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_api_tokens_workspace_id_id")),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "actor_type IN ('user', 'token', 'system')", name=op.f("ck_audit_log_actor_type")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_audit_log_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_audit_log_workspace_id_id")),
    )
    op.create_table(
        "datasets",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_datasets_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datasets")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_datasets_workspace_id_id")),
        sa.UniqueConstraint("workspace_id", "name", name=op.f("uq_datasets_workspace_id_name")),
    )
    op.create_table(
        "invites",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by_user_id", sa.Text(), nullable=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint("role IN ('owner', 'member', 'viewer')", name=op.f("ck_invites_role")),
        sa.ForeignKeyConstraint(
            ["accepted_by_user_id"], ["users.id"], name=op.f("fk_invites_accepted_by_user_id")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], name=op.f("fk_invites_created_by_user_id")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_invites_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invites")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_invites_token_hash")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_invites_workspace_id_id")),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_token", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'cancelled')",
            name=op.f("ck_jobs_status"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_jobs_attempts")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_jobs_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_jobs_workspace_id_id")),
    )
    op.create_index("ix_jobs_claimable", "jobs", ["status", "available_at"], unique=False)
    op.create_index(
        "uq_jobs_pending_work",
        "jobs",
        ["workspace_id", "kind", "subject_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "memberships",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "role IN ('owner', 'member', 'viewer')", name=op.f("ck_memberships_role")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_memberships_user_id")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_memberships_workspace_id")
        ),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", name=op.f("pk_memberships")),
    )
    op.create_table(
        "model_calls",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("reservation_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("requested_model", sa.Text(), nullable=False),
        sa.Column("resolved_model", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_number", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("other_tokens", sa.Integer(), nullable=True),
        sa.Column("usage_known", sa.Boolean(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ok', 'rate_limited', 'retryable_error', 'fatal_error', 'content_blocked')",
            name=op.f("ck_model_calls_status"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_model_calls_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_calls")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_model_calls_workspace_id_id")),
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
        sa.Column("requests_per_minute", sa.Integer(), nullable=True),
        sa.Column("tokens_per_minute", sa.BigInteger(), nullable=True),
        sa.Column("bucket_requests", sa.Double(), nullable=True),
        sa.Column("bucket_tokens", sa.Double(), nullable=True),
        sa.Column("bucket_refreshed_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_table(
        "dataset_versions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("dataset_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "dataset_id"],
            ["datasets.workspace_id", "datasets.id"],
            name=op.f("fk_dataset_versions_workspace_id_dataset_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_dataset_versions_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_versions")),
        sa.UniqueConstraint(
            "dataset_id", "version", name=op.f("uq_dataset_versions_dataset_id_version")
        ),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_dataset_versions_workspace_id_id")),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("csrf_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["memberships.workspace_id", "memberships.user_id"],
            name=op.f("fk_sessions_workspace_id_user_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_sessions_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_sessions_workspace_id_id")),
    )
    op.create_table(
        "experiments",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("dataset_version_id", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("split", sa.Text(), nullable=False),
        sa.Column("final", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("repetitions", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint("mode IN ('real', 'demo')", name=op.f("ck_experiments_mode")),
        sa.CheckConstraint(
            "split IN ('dev', 'validation', 'held_out')", name=op.f("ck_experiments_split")
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'paused_budget', 'paused_user', 'completed', 'closed_partial', 'cancelled')",
            name=op.f("ck_experiments_status"),
        ),
        sa.CheckConstraint("repetitions >= 1", name=op.f("ck_experiments_repetitions")),
        sa.ForeignKeyConstraint(
            ["workspace_id", "dataset_version_id"],
            ["dataset_versions.workspace_id", "dataset_versions.id"],
            name=op.f("fk_experiments_workspace_id_dataset_version_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_experiments_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiments")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_experiments_workspace_id_id")),
        sa.UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name=op.f("uq_experiments_workspace_id_idempotency_key"),
        ),
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
        sa.Column("job_id", sa.Text(), nullable=True),
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
    op.drop_table("spend_ledger")
    op.drop_table("budgets")
    op.drop_table("experiments")
    op.drop_table("sessions")
    op.drop_table("dataset_versions")
    op.drop_table("provider_credentials")
    op.drop_table("price_versions")
    op.drop_table("model_calls")
    op.drop_table("memberships")
    op.drop_index(
        "uq_jobs_pending_work",
        table_name="jobs",
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_index("ix_jobs_claimable", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("invites")
    op.drop_table("datasets")
    op.drop_table("audit_log")
    op.drop_table("api_tokens")
    op.drop_table("workspaces")
    op.drop_table("users")
