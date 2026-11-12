"""add traces, runs, spans, agents and versions

Revision ID: 76e9308e7b1f
Revises: 880b4102ccce
Create Date: 2026-11-12 07:04:37.223975
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "76e9308e7b1f"
down_revision: str | Sequence[str] | None = "880b4102ccce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agents",
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
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_agents_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agents")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_agents_workspace_id_id")),
        sa.UniqueConstraint("workspace_id", "name", name=op.f("uq_agents_workspace_id_name")),
    )
    op.create_table(
        "traces",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('received', 'normalized', 'failed')", name=op.f("ck_traces_status")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_traces_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_traces")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_traces_workspace_id_id")),
        sa.UniqueConstraint(
            "workspace_id", "trace_id", name=op.f("uq_traces_workspace_id_trace_id")
        ),
    )
    op.create_table(
        "agent_versions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agents.workspace_id", "agents.id"],
            name=op.f("fk_agent_versions_workspace_id_agent_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_agent_versions_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_versions")),
        sa.UniqueConstraint(
            "agent_id", "fingerprint", name=op.f("uq_agent_versions_agent_id_fingerprint")
        ),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_agent_versions_workspace_id_id")),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("trace_row_id", sa.Text(), nullable=False),
        sa.Column("agent_version_id", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("environment", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False),
        sa.Column("cost_known", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_version_id"],
            ["agent_versions.workspace_id", "agent_versions.id"],
            name=op.f("fk_runs_workspace_id_agent_version_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "trace_row_id"],
            ["traces.workspace_id", "traces.id"],
            name=op.f("fk_runs_workspace_id_trace_row_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_runs_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        sa.UniqueConstraint("trace_row_id", name=op.f("uq_runs_trace_row_id")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_runs_workspace_id_id")),
    )
    op.create_index(
        "ix_runs_version_started", "runs", ["agent_version_id", "started_at"], unique=False
    )
    op.create_table(
        "spans",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("span_id", sa.Text(), nullable=False),
        sa.Column("parent_span_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN ('model', 'tool', 'step')", name=op.f("ck_spans_kind")),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            name=op.f("fk_spans_workspace_id_run_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_spans_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spans")),
        sa.UniqueConstraint("run_id", "span_id", name=op.f("uq_spans_run_id_span_id")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_spans_workspace_id_id")),
    )


def downgrade() -> None:
    op.drop_table("spans")
    op.drop_index("ix_runs_version_started", table_name="runs")
    op.drop_table("runs")
    op.drop_table("agent_versions")
    op.drop_table("traces")
    op.drop_table("agents")
