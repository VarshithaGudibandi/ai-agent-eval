from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db import models
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.traces.normalize import NormalizedRun


async def store_raw(
    scope: WorkspaceScope, trace_id: str, agent_name: str, payload: dict[str, Any]
) -> models.Trace | None:
    """Keep a trace as received. Returns None if this workspace already has it."""
    row_id = await scope.session.scalar(
        insert(models.Trace)
        .values(
            id=new_id("trc"),
            workspace_id=scope.workspace_id,
            trace_id=trace_id,
            agent_name=agent_name,
            payload=payload,
            status=models.TraceStatus.RECEIVED,
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "trace_id"])
        .returning(models.Trace.id)
    )
    return None if row_id is None else await scope.require(models.Trace, row_id)


async def trace_known(scope: WorkspaceScope, trace_id: str) -> bool:
    found = await scope.first(scope.query(models.Trace).where(models.Trace.trace_id == trace_id))
    return found is not None


async def agent_version(
    scope: WorkspaceScope,
    agent_name: str,
    fingerprint: str,
    config: dict[str, Any],
    label: str | None,
) -> models.AgentVersion:
    """The version with this fingerprint, created along with its agent on first sight.

    Concurrent first traces of a new version race safely: the database keeps one row.
    """
    ws = scope.workspace_id
    await scope.session.execute(
        insert(models.Agent)
        .values(id=new_id("agt"), workspace_id=ws, name=agent_name)
        .on_conflict_do_nothing(index_elements=["workspace_id", "name"])
    )
    agent_id = await scope.session.scalar(
        select(models.Agent.id).where(
            models.Agent.workspace_id == ws, models.Agent.name == agent_name
        )
    )
    await scope.session.execute(
        insert(models.AgentVersion)
        .values(
            id=new_id("ver"),
            workspace_id=ws,
            agent_id=agent_id,
            fingerprint=fingerprint,
            config=config,
            label=label,
        )
        .on_conflict_do_nothing(index_elements=["agent_id", "fingerprint"])
    )
    version = await scope.first(
        scope.query(models.AgentVersion).where(
            models.AgentVersion.agent_id == agent_id,
            models.AgentVersion.fingerprint == fingerprint,
        )
    )
    if version is None:
        raise LookupError(fingerprint)
    # A label given later names a version that had none. One already set is never changed
    # by a trace; renaming a version is a deliberate act through the API.
    if label is not None and version.label is None:
        version.label = label
    return version


@dataclass(frozen=True)
class ExperimentLink:
    """Where a run sits in an experiment: which case, and which repetition of it."""

    experiment_id: str
    case_id: str
    repetition: int


async def save_run(
    scope: WorkspaceScope,
    trace: models.Trace,
    version: models.AgentVersion,
    tags: dict[str, Any],
    run: NormalizedRun,
    link: ExperimentLink | None = None,
) -> models.Run:
    row = models.Run(
        id=new_id("run"),
        workspace_id=scope.workspace_id,
        trace_row_id=trace.id,
        agent_version_id=version.id,
        experiment_id=link.experiment_id if link else None,
        case_id=link.case_id if link else None,
        repetition=link.repetition if link else None,
        tags=tags,
        environment=run.environment,
        session_id=run.session_id,
        started_at=run.started_at,
        ended_at=run.ended_at,
        duration_ms=run.duration_ms,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cost_micros=run.cost_micros,
        cost_known=run.cost_known,
    )
    scope.add(row)
    await scope.flush()
    for span in run.spans:
        scope.add(
            models.Span(
                id=new_id("spn"),
                workspace_id=scope.workspace_id,
                run_id=row.id,
                span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                kind=span.kind,
                name=span.name,
                started_at=span.started_at,
                ended_at=span.ended_at,
                duration_ms=span.duration_ms,
                input=span.input,
                output=span.output,
                error=span.error,
                attributes=span.attributes,
            )
        )
    trace.status = models.TraceStatus.NORMALIZED
    trace.error = None
    return row


async def run_for_trace(scope: WorkspaceScope, trace: models.Trace) -> models.Run | None:
    return await scope.first(scope.query(models.Run).where(models.Run.trace_row_id == trace.id))


async def spans_of(scope: WorkspaceScope, run_id: str) -> list[models.Span]:
    return await scope.all(
        scope.query(models.Span)
        .where(models.Span.run_id == run_id)
        .order_by(models.Span.started_at, models.Span.span_id)
    )
