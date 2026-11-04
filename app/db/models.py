from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        datetime: DateTime(timezone=True),
        str: Text,
    }


class ExperimentMode(StrEnum):
    REAL = "real"
    DEMO = "demo"


class ExperimentStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED_BUDGET = "paused_budget"
    PAUSED_USER = "paused_user"
    COMPLETED = "completed"
    CLOSED_PARTIAL = "closed_partial"
    CANCELLED = "cancelled"


class ExecutionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    BUDGET_EXCEEDED = "budget_exceeded"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class EvaluationStatus(StrEnum):
    PENDING = "pending"
    PASS = "pass"  # noqa: S105
    FAIL = "fail"
    PARTIAL = "partial"
    NOT_APPLICABLE = "not_applicable"
    EVALUATION_ERROR = "evaluation_error"


class Split(StrEnum):
    DEV = "dev"
    VALIDATION = "validation"
    HELD_OUT = "held_out"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"


class AdapterType(StrEnum):
    SCRIPTED = "scripted"
    TOOL_LOOP = "tool_loop"
    HTTP = "http"


class EvaluatorLayer(StrEnum):
    OUTCOME = "outcome"
    POLICY = "policy"
    TRAJECTORY = "trajectory"
    RESPONSE = "response"


class ToolCallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    REJECTED = "rejected"


class Role(StrEnum):
    OWNER = "owner"
    MEMBER = "member"
    VIEWER = "viewer"


class ActorType(StrEnum):
    USER = "user"
    TOKEN = "token"  # noqa: S105
    SYSTEM = "system"


class LedgerKind(StrEnum):
    RESERVATION = "reservation"
    RECONCILIATION = "reconciliation"


class ModelCallStatus(StrEnum):
    OK = "ok"
    RATE_LIMITED = "rate_limited"
    RETRYABLE_ERROR = "retryable_error"
    FATAL_ERROR = "fatal_error"
    CONTENT_BLOCKED = "content_blocked"


class KeyedProvider(StrEnum):
    GEMINI = "gemini"


class SnapshotKind(StrEnum):
    INITIAL = "initial"
    FINAL = "final"


def one_of(column: str, values: type[StrEnum]) -> CheckConstraint:
    allowed = ", ".join(f"'{v.value}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=column)


def workspace_fk(column: str, table: str) -> ForeignKeyConstraint:
    # Every child row names its parent together with the workspace, so the database
    # itself refuses a row that points into another workspace.
    return ForeignKeyConstraint(["workspace_id", column], [f"{table}.workspace_id", f"{table}.id"])


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class WorkspaceScoped:
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))


class User(Base):
    """A person. The only table without a workspace, since one person can join many."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Membership(WorkspaceScoped, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        PrimaryKeyConstraint("workspace_id", "user_id"),
        one_of("role", Role),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class UserSession(WorkspaceScoped, Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        # Removing someone from a workspace ends their sessions in it at once.
        ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["memberships.workspace_id", "memberships.user_id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[str]
    token_hash: Mapped[str] = mapped_column(unique=True)
    csrf_hash: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


class ApiToken(WorkspaceScoped, Base):
    __tablename__ = "api_tokens"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        one_of("role", Role),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    role: Mapped[str]
    token_hash: Mapped[str] = mapped_column(unique=True)
    token_prefix: Mapped[str]
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


class Invite(WorkspaceScoped, Base):
    __tablename__ = "invites"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        one_of("role", Role),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    role: Mapped[str]
    token_hash: Mapped[str] = mapped_column(unique=True)
    created_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]
    accepted_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))


class AuditEntry(WorkspaceScoped, Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        one_of("actor_type", ActorType),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    actor_type: Mapped[str]
    actor_id: Mapped[str]
    action: Mapped[str]
    target_type: Mapped[str]
    target_id: Mapped[str | None]
    details: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Dataset(WorkspaceScoped, Base):
    __tablename__ = "datasets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "name"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DatasetVersion(WorkspaceScoped, Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("dataset_id", "version"),
        workspace_fk("dataset_id", "datasets"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    dataset_id: Mapped[str]
    version: Mapped[int]
    content_hash: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TaskVersion(WorkspaceScoped, Base):
    __tablename__ = "task_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("dataset_version_id", "task_key"),
        workspace_fk("dataset_version_id", "dataset_versions"),
        one_of("split", Split),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    dataset_version_id: Mapped[str]
    task_key: Mapped[str]
    environment: Mapped[str]
    scenario_family: Mapped[str]
    split: Mapped[str]
    agent_input: Mapped[dict[str, Any]]
    fixture: Mapped[dict[str, Any]]
    evaluator_spec: Mapped[dict[str, Any]]
    limits: Mapped[dict[str, Any]]
    faults: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'"))
    category: Mapped[str]
    difficulty: Mapped[str]
    # Null until a person has reviewed the task. Unreviewed tasks run only in dev.
    reviewer: Mapped[str | None]
    review: Mapped[dict[str, Any]]
    content_hash: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentConfig(WorkspaceScoped, Base):
    __tablename__ = "agent_configs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        one_of("adapter_type", AdapterType),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    adapter_type: Mapped[str]
    settings: Mapped[dict[str, Any]]
    limits: Mapped[dict[str, Any]]
    config_hash: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Experiment(WorkspaceScoped, Base):
    __tablename__ = "experiments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "idempotency_key"),
        workspace_fk("dataset_version_id", "dataset_versions"),
        one_of("mode", ExperimentMode),
        one_of("status", ExperimentStatus),
        one_of("split", Split),
        CheckConstraint("repetitions >= 1", name="repetitions"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    dataset_version_id: Mapped[str]
    mode: Mapped[str]
    split: Mapped[str]
    # Only a final experiment may run held-out tasks.
    final: Mapped[bool]
    status: Mapped[str]
    repetitions: Mapped[int]
    idempotency_key: Mapped[str | None]
    manifest: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Run(WorkspaceScoped, Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint(
            "experiment_id",
            "agent_config_id",
            "task_version_id",
            "repetition",
            name="uq_runs_experiment_config_task_repetition",
        ),
        workspace_fk("experiment_id", "experiments"),
        workspace_fk("agent_config_id", "agent_configs"),
        workspace_fk("task_version_id", "task_versions"),
        one_of("execution_status", ExecutionStatus),
        one_of("evaluation_status", EvaluationStatus),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    experiment_id: Mapped[str]
    agent_config_id: Mapped[str]
    task_version_id: Mapped[str]
    repetition: Mapped[int]
    execution_status: Mapped[str]
    evaluation_status: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Attempt(WorkspaceScoped, Base):
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "env_id"),
        UniqueConstraint("run_id", "attempt_number"),
        UniqueConstraint("env_id"),
        workspace_fk("run_id", "runs"),
        one_of("execution_status", ExecutionStatus),
        Index("ix_attempts_lease", "execution_status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    run_id: Mapped[str]
    attempt_number: Mapped[int]
    env_id: Mapped[str]
    execution_status: Mapped[str]
    stop_reason: Mapped[str | None]
    final_response: Mapped[str | None]
    last_event_sequence: Mapped[int] = mapped_column(default=0, server_default="0")
    # Whoever holds this token may change the attempt, until the lease expires. Clearing it
    # revokes the attempt: every later step by its worker is refused.
    lease_token: Mapped[str | None]
    lease_expires_at: Mapped[datetime | None]
    worker_id: Mapped[str | None]
    # An external agent's credential for the gateway. Only its hash is kept, and it works only
    # while the lease above holds, so losing or revoking the lease also locks the agent out.
    run_token_hash: Mapped[str | None]
    run_token_expires_at: Mapped[datetime | None]
    # What an external agent posted back after answering 202 to the worker's request.
    external_result: Mapped[dict[str, Any] | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Job(WorkspaceScoped, Base):
    """One run waiting for, or held by, a worker. Postgres is the queue."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("run_id"),
        workspace_fk("run_id", "runs"),
        workspace_fk("experiment_id", "experiments"),
        one_of("status", JobStatus),
        Index("ix_jobs_claimable", "status", "available_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    experiment_id: Mapped[str]
    run_id: Mapped[str]
    status: Mapped[str]
    # Claim order inside an experiment. Configs alternate, so a provider slowdown hits every
    # config alike.
    position: Mapped[int]
    available_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TraceEvent(WorkspaceScoped, Base):
    __tablename__ = "trace_events"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("attempt_id", "sequence_number"),
        workspace_fk("attempt_id", "attempts"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    attempt_id: Mapped[str]
    sequence_number: Mapped[int]
    event_type: Mapped[str]
    payload: Mapped[dict[str, Any]]
    occurred_at: Mapped[datetime]
    elapsed_ms: Mapped[int]


class ToolCall(WorkspaceScoped, Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        workspace_fk("attempt_id", "attempts"),
        workspace_fk("request_event_id", "trace_events"),
        workspace_fk("result_event_id", "trace_events"),
        one_of("status", ToolCallStatus),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    attempt_id: Mapped[str]
    request_event_id: Mapped[str]
    result_event_id: Mapped[str]
    tool_name: Mapped[str]
    arguments: Mapped[dict[str, Any]]
    status: Mapped[str]
    error_code: Mapped[str | None]
    output: Mapped[dict[str, Any]]
    duration_ms: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EnvRecord(WorkspaceScoped, Base):
    """One record of an environment's live state, such as an order or a refund."""

    __tablename__ = "env_records"
    __table_args__ = (
        PrimaryKeyConstraint("env_id", "collection", "record_id"),
        ForeignKeyConstraint(
            ["workspace_id", "env_id"], ["attempts.workspace_id", "attempts.env_id"]
        ),
    )

    env_id: Mapped[str]
    collection: Mapped[str]
    record_id: Mapped[str]
    data: Mapped[dict[str, Any]]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class EnvSnapshot(WorkspaceScoped, Base):
    __tablename__ = "env_snapshots"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("attempt_id", "kind"),
        workspace_fk("attempt_id", "attempts"),
        one_of("kind", SnapshotKind),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    attempt_id: Mapped[str]
    kind: Mapped[str]
    state: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Score(WorkspaceScoped, Base):
    __tablename__ = "scores"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint(
            "attempt_id",
            "evaluator_name",
            "evaluator_version",
            "dimension",
            name="uq_scores_attempt_evaluator_dimension",
        ),
        workspace_fk("attempt_id", "attempts"),
        one_of("layer", EvaluatorLayer),
        one_of("status", EvaluationStatus),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    attempt_id: Mapped[str]
    evaluator_name: Mapped[str]
    evaluator_version: Mapped[str]
    layer: Mapped[str]
    dimension: Mapped[str]
    applicable: Mapped[bool]
    status: Mapped[str]
    score: Mapped[float | None]
    reason_code: Mapped[str | None]
    evidence: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ProviderCredential(WorkspaceScoped, Base):
    """A provider API key, encrypted. Only its last four characters are ever shown."""

    __tablename__ = "provider_credentials"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        one_of("provider", KeyedProvider),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    provider: Mapped[str]
    label: Mapped[str]
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    key_last4: Mapped[str]
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None]
    # A token bucket shared by every worker using this key. Null limits mean no limit.
    requests_per_minute: Mapped[int | None]
    tokens_per_minute: Mapped[int | None] = mapped_column(BigInteger)
    bucket_requests: Mapped[float | None]
    bucket_tokens: Mapped[float | None]
    bucket_refreshed_at: Mapped[datetime | None]


class PriceVersion(WorkspaceScoped, Base):
    """Prices for one model, per million tokens, in micro-units. Rows are never edited."""

    __tablename__ = "price_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        CheckConstraint(
            "input_per_million_micros >= 0 AND cached_input_per_million_micros >= 0"
            " AND output_per_million_micros >= 0 AND other_per_million_micros >= 0",
            name="prices_not_negative",
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    provider: Mapped[str]
    model: Mapped[str]
    currency: Mapped[str]
    input_per_million_micros: Mapped[int] = mapped_column(BigInteger)
    cached_input_per_million_micros: Mapped[int] = mapped_column(BigInteger)
    output_per_million_micros: Mapped[int] = mapped_column(BigInteger)
    other_per_million_micros: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Budget(WorkspaceScoped, Base):
    """Limits and running totals for one experiment. A null limit means no cap.

    The used totals include amounts reserved for calls still in flight.
    """

    __tablename__ = "budgets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("experiment_id"),
        workspace_fk("experiment_id", "experiments"),
        CheckConstraint(
            "money_used_micros >= 0 AND tokens_used >= 0 AND requests_used >= 0",
            name="used_not_negative",
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    experiment_id: Mapped[str]
    currency: Mapped[str]
    money_limit_micros: Mapped[int | None] = mapped_column(BigInteger)
    tokens_limit: Mapped[int | None] = mapped_column(BigInteger)
    requests_limit: Mapped[int | None] = mapped_column(BigInteger)
    money_used_micros: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    tokens_used: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    requests_used: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    warned_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class SpendEntry(WorkspaceScoped, Base):
    __tablename__ = "spend_ledger"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        workspace_fk("budget_id", "budgets"),
        one_of("kind", LedgerKind),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    budget_id: Mapped[str]
    attempt_id: Mapped[str | None]
    reservation_id: Mapped[str | None]
    kind: Mapped[str]
    money_micros: Mapped[int] = mapped_column(BigInteger)
    tokens: Mapped[int] = mapped_column(BigInteger)
    requests: Mapped[int] = mapped_column(BigInteger)
    cost_known: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ModelCall(WorkspaceScoped, Base):
    """One request to a model provider, including retries, with what it used and cost."""

    __tablename__ = "model_calls"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        workspace_fk("attempt_id", "attempts"),
        workspace_fk("trace_event_id", "trace_events"),
        one_of("status", ModelCallStatus),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    attempt_id: Mapped[str]
    trace_event_id: Mapped[str]
    reservation_id: Mapped[str]
    provider: Mapped[str]
    requested_model: Mapped[str]
    resolved_model: Mapped[str | None]
    status: Mapped[str]
    error: Mapped[str | None]
    retry_number: Mapped[int]
    input_tokens: Mapped[int | None]
    cached_input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    other_tokens: Mapped[int | None]
    usage_known: Mapped[bool]
    self_reported: Mapped[bool] = mapped_column(default=False, server_default="false")
    cost_micros: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
