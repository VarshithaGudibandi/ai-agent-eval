from datetime import date, datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
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


class ExperimentStatus(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"
    PAUSED_BUDGET = "paused_budget"


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


class ScorerKind(StrEnum):
    BUILTIN = "builtin"
    RULE = "rule"
    JUDGE = "judge"


class ScorerScope(StrEnum):
    RUN = "run"
    STEP = "step"


class Severity(StrEnum):
    HARD = "hard"
    SOFT = "soft"


class VerdictStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    # Nothing failed, but a scorer could not run, so the run cannot be called a pass.
    INCOMPLETE = "incomplete"
    # No scorer applied to the run.
    NOT_SCORED = "not_scored"


class JudgeAnswerValue(StrEnum):
    YES = "yes"
    NO = "no"
    CANT_TELL = "cant_tell"


class HumanAnswer(StrEnum):
    YES = "yes"
    NO = "no"


class ReviewReason(StrEnum):
    # Picked at random from judged runs, so labels cover ordinary runs too.
    SAMPLED = "sampled"
    # The judge was unsure.
    BORDERLINE = "borderline"
    MANUAL = "manual"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    LABELED = "labeled"


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


class DatasetCase(WorkspaceScoped, Base):
    """One case in a dataset version: what the agent is given and, optionally, what should
    happen. Versions are immutable, so a case never changes once written."""

    __tablename__ = "dataset_cases"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        # The key names the same case across versions, so results can be paired by case.
        UniqueConstraint("dataset_version_id", "case_key"),
        workspace_fk("dataset_version_id", "dataset_versions"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    dataset_version_id: Mapped[str]
    case_key: Mapped[str]
    position: Mapped[int]
    input: Mapped[Any] = mapped_column(JSONB)
    expected: Mapped[Any | None] = mapped_column(JSONB)
    tags: Mapped[dict[str, Any]]
    notes: Mapped[str | None]


class Experiment(WorkspaceScoped, Base):
    """One agent version run over a dataset version, each case a number of times."""

    __tablename__ = "experiments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "idempotency_key"),
        workspace_fk("dataset_version_id", "dataset_versions"),
        workspace_fk("agent_version_id", "agent_versions"),
        one_of("status", ExperimentStatus),
        CheckConstraint("repetitions >= 1", name="repetitions"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    agent_version_id: Mapped[str]
    dataset_version_id: Mapped[str]
    status: Mapped[str]
    repetitions: Mapped[int]
    # Sent by the SDK, so a retried start returns the same experiment instead of a second one.
    idempotency_key: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]


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
    # When a new version is worse enough to hold back; empty means the defaults.
    gates: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    # The version last marked as shipped, which comparisons use as the baseline. Checked
    # by the API; not a foreign key, as agent_versions already points at agents.
    shipped_version_id: Mapped[str | None]
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
        workspace_fk("experiment_id", "experiments"),
        workspace_fk("case_id", "dataset_cases"),
        UniqueConstraint("experiment_id", "case_id", "repetition"),
        CheckConstraint("repetition >= 1", name="repetition"),
        Index("ix_runs_version_started", "agent_version_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    trace_row_id: Mapped[str]
    agent_version_id: Mapped[str]
    # Set for runs of an experiment; live runs have none.
    experiment_id: Mapped[str | None]
    case_id: Mapped[str | None]
    repetition: Mapped[int | None]
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


class Feedback(WorkspaceScoped, Base):
    """A user's rating of one trace, sent from the team's own product.

    trace_id is not a foreign key: in live mode the feedback can arrive before its trace.
    """

    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "feedback_id"),
        CheckConstraint("score >= 0 AND score <= 1", name="score_range"),
        Index("ix_feedback_workspace_id_trace_id", "workspace_id", "trace_id"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    # Chosen by the sender, so a retried send is stored once.
    feedback_id: Mapped[str]
    trace_id: Mapped[str]
    score: Mapped[float]
    comment: Mapped[str | None]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EvalSpec(WorkspaceScoped, Base):
    """One version of an agent's eval spec: the scorers that grade its runs."""

    __tablename__ = "eval_specs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("agent_id", "version"),
        workspace_fk("agent_id", "agents"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    agent_id: Mapped[str]
    version: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Scorer(WorkspaceScoped, Base):
    """One version of one scorer. Never changed: a different scorer is a new version, and
    an unchanged one is shared by every spec version that keeps it, so its scores stay
    comparable across them."""

    __tablename__ = "scorers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("agent_id", "key", "content_hash"),
        UniqueConstraint("agent_id", "key", "version"),
        workspace_fk("agent_id", "agents"),
        one_of("kind", ScorerKind),
        one_of("scope", ScorerScope),
        one_of("severity", Severity),
        CheckConstraint("threshold >= 0 AND threshold <= 1", name="threshold_range"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    agent_id: Mapped[str]
    key: Mapped[str]
    version: Mapped[int]
    check: Mapped[str]
    kind: Mapped[str]
    scope: Mapped[str]
    severity: Mapped[str]
    threshold: Mapped[float]
    params: Mapped[dict[str, Any]]
    applies_when: Mapped[dict[str, Any]]
    content_hash: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EvalSpecScorer(WorkspaceScoped, Base):
    __tablename__ = "eval_spec_scorers"
    __table_args__ = (
        PrimaryKeyConstraint("eval_spec_id", "scorer_id"),
        UniqueConstraint("eval_spec_id", "position"),
        workspace_fk("eval_spec_id", "eval_specs"),
        workspace_fk("scorer_id", "scorers"),
    )

    eval_spec_id: Mapped[str]
    scorer_id: Mapped[str]
    position: Mapped[int]


class Score(WorkspaceScoped, Base):
    """One scorer's result for a run, or for one span of it when span_id is set."""

    __tablename__ = "scores"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        # A rerun of the same scorer on the same target never adds a second score.
        UniqueConstraint("run_id", "span_id", "scorer_id", postgresql_nulls_not_distinct=True),
        workspace_fk("run_id", "runs"),
        workspace_fk("scorer_id", "scorers"),
        ForeignKeyConstraint(["run_id", "span_id"], ["spans.run_id", "spans.span_id"]),
        CheckConstraint("value >= 0 AND value <= 1", name="value_range"),
        # Either a result or an error: a scorer that crashed neither passed nor failed.
        CheckConstraint(
            "(error IS NULL AND value IS NOT NULL AND passed IS NOT NULL)"
            " OR (error IS NOT NULL AND value IS NULL AND passed IS NULL)",
            name="result_or_error",
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    run_id: Mapped[str]
    span_id: Mapped[str | None]
    scorer_id: Mapped[str]
    value: Mapped[float | None]
    passed: Mapped[bool | None]
    reason: Mapped[str]
    evidence: Mapped[list[str]] = mapped_column(JSONB)
    error: Mapped[str | None]
    # Judges only: how far repeated answers agreed. None when asked once.
    confidence: Mapped[float | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Verdict(WorkspaceScoped, Base):
    """A run's verdict under one eval spec version."""

    __tablename__ = "verdicts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("run_id", "eval_spec_id"),
        workspace_fk("run_id", "runs"),
        workspace_fk("eval_spec_id", "eval_specs"),
        one_of("status", VerdictStatus),
        CheckConstraint("quality >= 0 AND quality <= 1", name="quality_range"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    run_id: Mapped[str]
    eval_spec_id: Mapped[str]
    status: Mapped[str]
    # The average of the soft scores; none when no soft scorer applied.
    quality: Mapped[float | None]
    failed: Mapped[list[str]] = mapped_column(JSONB)
    errored: Mapped[list[str]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class JudgeAnswer(WorkspaceScoped, Base):
    """A judge's answer to one question about one run, with the quote it rests on."""

    __tablename__ = "judge_answers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("run_id", "scorer_id", "question_id"),
        workspace_fk("run_id", "runs"),
        workspace_fk("scorer_id", "scorers"),
        one_of("answer", JudgeAnswerValue),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    run_id: Mapped[str]
    scorer_id: Mapped[str]
    question_id: Mapped[str]
    question_text: Mapped[str]
    # Calibration follows the wording: a reworded question starts again from no labels.
    question_hash: Mapped[str]
    answer: Mapped[str]
    quote: Mapped[str | None]
    evidence: Mapped[list[str]] = mapped_column(JSONB)
    confidence: Mapped[float | None]
    # Whether the question was trusted when this answer was given, so counted in the score.
    trusted: Mapped[bool]
    model_version: Mapped[str]
    # The claim check's claims, each with its verdict and quote.
    details: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ReviewItem(WorkspaceScoped, Base):
    """A run waiting for, or given, a person's labels."""

    __tablename__ = "review_items"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("run_id"),
        workspace_fk("run_id", "runs"),
        one_of("reason", ReviewReason),
        one_of("status", ReviewStatus),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    run_id: Mapped[str]
    reason: Mapped[str]
    status: Mapped[str]
    note: Mapped[str | None]
    labeled_by: Mapped[str | None]
    labeled_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ReviewLabel(WorkspaceScoped, Base):
    """A person's yes or no to one judge question about one run."""

    __tablename__ = "review_labels"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("review_item_id", "scorer_id", "question_id"),
        workspace_fk("review_item_id", "review_items"),
        workspace_fk("run_id", "runs"),
        workspace_fk("scorer_id", "scorers"),
        one_of("answer", HumanAnswer),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    review_item_id: Mapped[str]
    run_id: Mapped[str]
    scorer_id: Mapped[str]
    question_id: Mapped[str]
    question_hash: Mapped[str]
    answer: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Comparison(WorkspaceScoped, Base):
    """Two sets of runs compared: two experiments, or two windows of live traffic."""

    __tablename__ = "comparisons"
    __table_args__ = (UniqueConstraint("workspace_id", "id"),)

    id: Mapped[str] = mapped_column(primary_key=True)
    # What each side is: {"experiment_id": ...} or {"agent_id", "version_id", "from", "to"}.
    baseline: Mapped[dict[str, Any]]
    candidate: Mapped[dict[str, Any]]
    gates: Mapped[dict[str, Any]]
    decision: Mapped[str]
    result: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Aggregate(WorkspaceScoped, Base):
    """Health of one agent version's live runs on one day, rebuilt from its verdicts."""

    __tablename__ = "aggregates"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("agent_version_id", "day"),
        workspace_fk("agent_version_id", "agent_versions"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    agent_version_id: Mapped[str]
    day: Mapped[date] = mapped_column(Date)
    runs: Mapped[int]
    health: Mapped[dict[str, Any]]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
