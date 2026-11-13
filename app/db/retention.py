from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.db.scope import WorkspaceScope

# Retention runs across every workspace, like the job queue's reconciler: each workspace
# sets its own limit, and one pass removes whatever text is past it.

TEXT_FIELDS = ("input", "output")


def without_text(payload: dict[str, Any]) -> dict[str, Any]:
    """The raw trace with every span's input and output removed, everything else kept."""
    spans = [{**s, **dict.fromkeys(TEXT_FIELDS)} for s in payload.get("spans", [])]
    return {**payload, "spans": spans}


async def remove_old_text(session: AsyncSession, batch: int = 200) -> int:
    """Remove the text of traces older than their workspace's limit, at most batch of them.
    Returns how many. Runs, timings, tokens, costs and scores stay.

    Call it until it returns less than batch to catch up. SKIP LOCKED lets two workers
    share the work without waiting on each other.
    """
    expired = (
        select(models.Trace)
        .join(models.Workspace, models.Workspace.id == models.Trace.workspace_id)
        .where(
            models.Workspace.retention_days.is_not(None),
            models.Trace.text_removed_at.is_(None),
            models.Trace.received_at
            < func.now() - func.make_interval(0, 0, 0, models.Workspace.retention_days),
        )
        .order_by(models.Trace.received_at)
        .limit(batch)
        .with_for_update(of=models.Trace, skip_locked=True)
    )
    traces = list(await session.scalars(expired))
    for trace in traces:
        trace.payload = without_text(trace.payload)
        trace.text_removed_at = func.now()
    if traces:
        runs = select(models.Run.id).where(models.Run.trace_row_id.in_([t.id for t in traces]))
        await session.execute(
            update(models.Span).where(models.Span.run_id.in_(runs)).values(input=None, output=None)
        )
    return len(traces)


async def set_retention(scope: WorkspaceScope, days: int | None) -> models.Workspace:
    workspace = await scope.session.get_one(models.Workspace, scope.workspace_id)
    workspace.retention_days = days
    return workspace
