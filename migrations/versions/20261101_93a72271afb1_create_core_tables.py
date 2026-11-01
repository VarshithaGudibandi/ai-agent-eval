"""create core tables

Revision ID: 93a72271afb1
Revises:
Create Date: 2026-11-01 16:02:54.755185
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "93a72271afb1"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
        "agent_configs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("adapter_type", sa.Text(), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "adapter_type IN ('scripted', 'tool_loop', 'http')",
            name=op.f("ck_agent_configs_adapter_type"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_agent_configs_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_configs")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_agent_configs_workspace_id_id")),
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
        "experiments",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("dataset_version_id", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
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
        "task_versions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("dataset_version_id", sa.Text(), nullable=False),
        sa.Column("task_key", sa.Text(), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("scenario_family", sa.Text(), nullable=False),
        sa.Column("split", sa.Text(), nullable=False),
        sa.Column("agent_input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fixture", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evaluator_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "split IN ('dev', 'validation', 'held_out')", name=op.f("ck_task_versions_split")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "dataset_version_id"],
            ["dataset_versions.workspace_id", "dataset_versions.id"],
            name=op.f("fk_task_versions_workspace_id_dataset_version_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_task_versions_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_versions")),
        sa.UniqueConstraint(
            "dataset_version_id",
            "task_key",
            name=op.f("uq_task_versions_dataset_version_id_task_key"),
        ),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_task_versions_workspace_id_id")),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("experiment_id", sa.Text(), nullable=False),
        sa.Column("agent_config_id", sa.Text(), nullable=False),
        sa.Column("task_version_id", sa.Text(), nullable=False),
        sa.Column("repetition", sa.Integer(), nullable=False),
        sa.Column("execution_status", sa.Text(), nullable=False),
        sa.Column("evaluation_status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "evaluation_status IN ('pending', 'pass', 'fail', 'partial', 'not_applicable', 'evaluation_error')",
            name=op.f("ck_runs_evaluation_status"),
        ),
        sa.CheckConstraint(
            "execution_status IN ('queued', 'running', 'completed', 'timed_out', 'budget_exceeded', 'infrastructure_error', 'interrupted', 'cancelled')",
            name=op.f("ck_runs_execution_status"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_config_id"],
            ["agent_configs.workspace_id", "agent_configs.id"],
            name=op.f("fk_runs_workspace_id_agent_config_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["experiments.workspace_id", "experiments.id"],
            name=op.f("fk_runs_workspace_id_experiment_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "task_version_id"],
            ["task_versions.workspace_id", "task_versions.id"],
            name=op.f("fk_runs_workspace_id_task_version_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_runs_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        sa.UniqueConstraint(
            "experiment_id",
            "agent_config_id",
            "task_version_id",
            "repetition",
            name="uq_runs_experiment_config_task_repetition",
        ),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_runs_workspace_id_id")),
    )
    op.create_table(
        "attempts",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("env_id", sa.Text(), nullable=False),
        sa.Column("execution_status", sa.Text(), nullable=False),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("final_response", sa.Text(), nullable=True),
        sa.Column("last_event_sequence", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "execution_status IN ('queued', 'running', 'completed', 'timed_out', 'budget_exceeded', 'infrastructure_error', 'interrupted', 'cancelled')",
            name=op.f("ck_attempts_execution_status"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            name=op.f("fk_attempts_workspace_id_run_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_attempts_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attempts")),
        sa.UniqueConstraint("env_id", name=op.f("uq_attempts_env_id")),
        sa.UniqueConstraint(
            "run_id", "attempt_number", name=op.f("uq_attempts_run_id_attempt_number")
        ),
        sa.UniqueConstraint("workspace_id", "env_id", name=op.f("uq_attempts_workspace_id_env_id")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_attempts_workspace_id_id")),
    )
    op.create_table(
        "env_records",
        sa.Column("env_id", sa.Text(), nullable=False),
        sa.Column("collection", sa.Text(), nullable=False),
        sa.Column("record_id", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "env_id"],
            ["attempts.workspace_id", "attempts.env_id"],
            name=op.f("fk_env_records_workspace_id_env_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_env_records_workspace_id")
        ),
        sa.PrimaryKeyConstraint("env_id", "collection", "record_id", name=op.f("pk_env_records")),
    )
    op.create_table(
        "env_snapshots",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN ('initial', 'final')", name=op.f("ck_env_snapshots_kind")),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["attempts.workspace_id", "attempts.id"],
            name=op.f("fk_env_snapshots_workspace_id_attempt_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_env_snapshots_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_env_snapshots")),
        sa.UniqueConstraint("attempt_id", "kind", name=op.f("uq_env_snapshots_attempt_id_kind")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_env_snapshots_workspace_id_id")),
    )
    op.create_table(
        "scores",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=False),
        sa.Column("evaluator_name", sa.Text(), nullable=False),
        sa.Column("evaluator_version", sa.Text(), nullable=False),
        sa.Column("layer", sa.Text(), nullable=False),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column("applicable", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("score", sa.Double(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "layer IN ('outcome', 'policy', 'trajectory', 'response')", name=op.f("ck_scores_layer")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'pass', 'fail', 'partial', 'not_applicable', 'evaluation_error')",
            name=op.f("ck_scores_status"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["attempts.workspace_id", "attempts.id"],
            name=op.f("fk_scores_workspace_id_attempt_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_scores_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scores")),
        sa.UniqueConstraint(
            "attempt_id",
            "evaluator_name",
            "evaluator_version",
            "dimension",
            name="uq_scores_attempt_evaluator_dimension",
        ),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_scores_workspace_id_id")),
    )
    op.create_table(
        "trace_events",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["attempts.workspace_id", "attempts.id"],
            name=op.f("fk_trace_events_workspace_id_attempt_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_trace_events_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trace_events")),
        sa.UniqueConstraint(
            "attempt_id", "sequence_number", name=op.f("uq_trace_events_attempt_id_sequence_number")
        ),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_trace_events_workspace_id_id")),
    )
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=False),
        sa.Column("request_event_id", sa.Text(), nullable=False),
        sa.Column("result_event_id", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ok', 'error', 'rejected')", name=op.f("ck_tool_calls_status")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["attempts.workspace_id", "attempts.id"],
            name=op.f("fk_tool_calls_workspace_id_attempt_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "request_event_id"],
            ["trace_events.workspace_id", "trace_events.id"],
            name=op.f("fk_tool_calls_workspace_id_request_event_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "result_event_id"],
            ["trace_events.workspace_id", "trace_events.id"],
            name=op.f("fk_tool_calls_workspace_id_result_event_id"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_tool_calls_workspace_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_tool_calls_workspace_id_id")),
    )


def downgrade() -> None:
    op.drop_table("tool_calls")
    op.drop_table("trace_events")
    op.drop_table("scores")
    op.drop_table("env_snapshots")
    op.drop_table("env_records")
    op.drop_table("attempts")
    op.drop_table("runs")
    op.drop_table("task_versions")
    op.drop_table("experiments")
    op.drop_table("dataset_versions")
    op.drop_table("datasets")
    op.drop_table("agent_configs")
    op.drop_table("workspaces")
