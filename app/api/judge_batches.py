from datetime import datetime
from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel

from app.api.dependencies import Db, Member, Viewer, scope_for
from app.db import models
from app.db.audit import record_audit
from app.db.judge_batches import batch_run_ids
from app.db.outcomes import batch_outcomes
from app.db.scope import WorkspaceScope
from app.metrics.health import Health, health
from app.scoring.judge_batches import NewJudgeBatch, start_batch

router = APIRouter(prefix="/api/v1/judge-batches", tags=["judging"])


class JudgeBatchOut(BaseModel):
    id: str
    agent_id: str
    agent_version_id: str | None
    window_start: datetime
    window_end: datetime
    tags: dict[str, Any]
    limit: int
    trigger: models.JudgeBatchTrigger
    status: models.JudgeBatchStatus
    created_at: datetime
    finished_at: datetime | None
    runs: int
    judged: int
    health: Health


async def _out(scope: WorkspaceScope, batch: models.JudgeBatch) -> JudgeBatchOut:
    runs, judged = await batch_run_ids(scope, batch.id)
    return JudgeBatchOut(
        id=batch.id,
        agent_id=batch.agent_id,
        agent_version_id=batch.agent_version_id,
        window_start=batch.window_start,
        window_end=batch.window_end,
        tags=batch.tags,
        limit=batch.run_limit,
        trigger=models.JudgeBatchTrigger(batch.trigger),
        status=models.JudgeBatchStatus(batch.status),
        created_at=batch.created_at,
        finished_at=batch.finished_at,
        runs=len(runs),
        judged=judged,
        health=health((await batch_outcomes(scope, batch.id)).outcomes),
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(body: NewJudgeBatch, principal: Member, session: Db) -> JudgeBatchOut:
    """Judge stored live runs: an agent's or one version's, in a time window, up to a limit.

    Needs a Gemini key marked paid, and judges in the agent's eval spec. The batch's budget
    covers its runs and nothing more.
    """
    scope = scope_for(session, principal)
    batch = await start_batch(scope, body, models.JudgeBatchTrigger.MANUAL)
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "judge_batch.started",
        "judge_batch",
        batch.id,
        {"limit": body.limit},
    )
    await scope.flush()
    await session.refresh(batch)
    return await _out(scope, batch)


@router.get("/{batch_id}")
async def batch(batch_id: str, principal: Viewer, session: Db) -> JudgeBatchOut:
    """A judge batch: its progress and the health of its runs."""
    scope = scope_for(session, principal)
    return await _out(scope, await scope.require(models.JudgeBatch, batch_id))
