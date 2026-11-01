"""add model calls and rate limit buckets

Revision ID: 17cb325c520b
Revises: 3bf18165f97e
Create Date: 2026-11-01 18:25:15.182693
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "17cb325c520b"
down_revision: str | Sequence[str] | None = "3bf18165f97e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_calls",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=False),
        sa.Column("trace_event_id", sa.Text(), nullable=False),
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
        sa.Column("self_reported", sa.Boolean(), server_default="false", nullable=False),
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
            ["workspace_id", "attempt_id"],
            ["attempts.workspace_id", "attempts.id"],
            name=op.f("fk_model_calls_workspace_id_attempt_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "trace_event_id"],
            ["trace_events.workspace_id", "trace_events.id"],
            name=op.f("fk_model_calls_workspace_id_trace_event_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_model_calls_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_calls")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_model_calls_workspace_id_id")),
    )
    op.add_column(
        "provider_credentials", sa.Column("requests_per_minute", sa.Integer(), nullable=True)
    )
    op.add_column(
        "provider_credentials", sa.Column("tokens_per_minute", sa.BigInteger(), nullable=True)
    )
    op.add_column("provider_credentials", sa.Column("bucket_requests", sa.Double(), nullable=True))
    op.add_column("provider_credentials", sa.Column("bucket_tokens", sa.Double(), nullable=True))
    op.add_column(
        "provider_credentials",
        sa.Column("bucket_refreshed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("provider_credentials", "bucket_refreshed_at")
    op.drop_column("provider_credentials", "bucket_tokens")
    op.drop_column("provider_credentials", "bucket_requests")
    op.drop_column("provider_credentials", "tokens_per_minute")
    op.drop_column("provider_credentials", "requests_per_minute")
    op.drop_table("model_calls")
