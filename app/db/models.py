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


class Split(StrEnum):
    DEV = "dev"
    VALIDATION = "validation"
    HELD_OUT = "held_out"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TraceStatus(StrEnum):
    RECEIVED = "received"
    NORMALIZED = "normalized"
    FAILED = "failed"


class SpanKind(StrEnum):
    MODEL = "model"
    TOOL = "tool"
    STEP = "step"


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
    # How many traces the workspace may still send this minute, refilled continuously.
    ingest_bucket: Mapped[float | None]
    ingest_bucket_refreshed_at: Mapped[datetime | None]
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


class Job(WorkspaceScoped, Base):
    """One piece of background work, such as scoring a trace. Postgres is the queue.

    The lease lives on the job: the worker holding a live lease is the only one allowed to
    write the job's results.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        one_of("status", JobStatus),
        CheckConstraint("attempts >= 0", name="attempts"),
        Index("ix_jobs_claimable", "status", "available_at"),
        # A retried request must not queue the same work twice while it is still pending.
        Index(
            "uq_jobs_pending_work",
            "workspace_id",
            "kind",
            "subject_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[str]
    # What the job is about, such as a trace id. Its meaning depends on the kind.
    subject_id: Mapped[str]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    status: Mapped[str]
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    lease_token: Mapped[str | None]
    lease_expires_at: Mapped[datetime | None]
    worker_id: Mapped[str | None]
    last_error: Mapped[str | None]
    available_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]


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
    # The job that spent it, so a stale worker's spending can be traced.
    job_id: Mapped[str | None]
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
        one_of("status", ModelCallStatus),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
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
    cost_micros: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Agent(WorkspaceScoped, Base):
    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "name"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentVersion(WorkspaceScoped, Base):
    """One configuration of an agent, found by the fingerprint of its config."""

    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("agent_id", "fingerprint"),
        workspace_fk("agent_id", "agents"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    agent_id: Mapped[str]
    fingerprint: Mapped[str]
    config: Mapped[dict[str, Any]]
    # A name a person gives the version, such as "v7". Optional: the fingerprint is enough.
    label: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Trace(WorkspaceScoped, Base):
    """A trace exactly as received, secrets masked. The payload is never edited, so every
    later stage can be run again from it."""

    __tablename__ = "traces"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "trace_id"),
        one_of("status", TraceStatus),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    # The id the agent's side chose, so a retried send is recognised as the same trace.
    trace_id: Mapped[str]
    agent_name: Mapped[str]
    payload: Mapped[dict[str, Any]]
    status: Mapped[str]
    error: Mapped[str | None]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Run(WorkspaceScoped, Base):
    """One agent run, built from its trace."""

    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("trace_row_id"),
        workspace_fk("trace_row_id", "traces"),
        workspace_fk("agent_version_id", "agent_versions"),
        Index("ix_runs_version_started", "agent_version_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    trace_row_id: Mapped[str]
    agent_version_id: Mapped[str]
    tags: Mapped[dict[str, Any]]
    environment: Mapped[str | None]
    session_id: Mapped[str | None]
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime]
    duration_ms: Mapped[int]
    input_tokens: Mapped[int] = mapped_column(BigInteger)
    output_tokens: Mapped[int] = mapped_column(BigInteger)
    cost_micros: Mapped[int] = mapped_column(BigInteger)
    # False when a model call had no price or no token counts, so the cost is a floor.
    cost_known: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Span(WorkspaceScoped, Base):
    __tablename__ = "spans"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("run_id", "span_id"),
        workspace_fk("run_id", "runs"),
        one_of("kind", SpanKind),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    run_id: Mapped[str]
    span_id: Mapped[str]
    parent_span_id: Mapped[str | None]
    kind: Mapped[str]
    name: Mapped[str]
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime]
    duration_ms: Mapped[int]
    input: Mapped[Any] = mapped_column(JSONB)
    output: Mapped[Any] = mapped_column(JSONB)
    error: Mapped[str | None]
    attributes: Mapped[dict[str, Any]]
