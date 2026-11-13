from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.db.scope import WorkspaceScope


async def live_run_ids(
    scope: WorkspaceScope,
    agent_id: str,
    version_id: str | None,
    start: datetime,
    end: datetime,
    tags: dict[str, Any],
    limit: int,
) -> list[str]:
    """Stored live runs that match, newest first."""
    statement = (
        select(models.Run.id)
        .join(models.AgentVersion, models.AgentVersion.id == models.Run.agent_version_id)
        .where(
            models.Run.workspace_id == scope.workspace_id,
            models.AgentVersion.agent_id == agent_id,
            models.Run.experiment_id.is_(None),
            models.Run.started_at >= start,
            models.Run.started_at < end,
        )
        .order_by(models.Run.started_at.desc())
        .limit(limit)
    )
    if version_id is not None:
        statement = statement.where(models.Run.agent_version_id == version_id)
    if tags:
        statement = statement.where(models.Run.tags.contains(tags))
    return list(await scope.session.scalars(statement))


async def add_batch_runs(scope: WorkspaceScope, batch_id: str, run_ids: list[str]) -> None:
    if not run_ids:
        return
    await scope.session.execute(
        insert(models.JudgeBatchRun)
        .values(
            [
                {"workspace_id": scope.workspace_id, "judge_batch_id": batch_id, "run_id": r}
                for r in run_ids
            ]
        )
        .on_conflict_do_nothing()
    )


async def in_running_batch(scope: WorkspaceScope, batch_id: str, run_id: str) -> bool:
    found = await scope.session.scalar(
        select(models.JudgeBatchRun.run_id)
        .join(models.JudgeBatch, models.JudgeBatch.id == models.JudgeBatchRun.judge_batch_id)
        .where(
            models.JudgeBatchRun.workspace_id == scope.workspace_id,
            models.JudgeBatchRun.judge_batch_id == batch_id,
            models.JudgeBatchRun.run_id == run_id,
            models.JudgeBatch.status == models.JudgeBatchStatus.RUNNING,
        )
    )
    return found is not None


async def mark_judged(scope: WorkspaceScope, run_id: str) -> None:
    """Record a run as judged in every batch that holds it, and finish each batch whose
    runs are all judged. Overlapping batches share one judging of a run, as only one
    judge job per run can wait at a time."""
    batch_ids = list(
        await scope.session.scalars(
            update(models.JudgeBatchRun)
            .where(
                models.JudgeBatchRun.workspace_id == scope.workspace_id,
                models.JudgeBatchRun.run_id == run_id,
                models.JudgeBatchRun.judged_at.is_(None),
            )
            .values(judged_at=func.now())
            .returning(models.JudgeBatchRun.judge_batch_id)
        )
    )
    if not batch_ids:
        return
    waiting = (
        select(models.JudgeBatchRun.run_id)
        .where(
            models.JudgeBatchRun.judge_batch_id == models.JudgeBatch.id,
            models.JudgeBatchRun.judged_at.is_(None),
        )
        .exists()
    )
    await scope.session.execute(
        update(models.JudgeBatch)
        .where(
            models.JudgeBatch.workspace_id == scope.workspace_id,
            models.JudgeBatch.id.in_(batch_ids),
            models.JudgeBatch.status == models.JudgeBatchStatus.RUNNING,
            ~waiting,
        )
        .values(status=models.JudgeBatchStatus.FINISHED, finished_at=func.now())
    )


async def pause_batch_for_budget(scope: WorkspaceScope, batch_id: str) -> bool:
    """Pause a running batch. Returns True only if this call paused it."""
    paused = await scope.session.scalar(
        update(models.JudgeBatch)
        .where(
            models.JudgeBatch.workspace_id == scope.workspace_id,
            models.JudgeBatch.id == batch_id,
            models.JudgeBatch.status == models.JudgeBatchStatus.RUNNING,
        )
        .values(status=models.JudgeBatchStatus.PAUSED_BUDGET)
        .returning(models.JudgeBatch.id)
    )
    return paused is not None


async def batch_run_ids(scope: WorkspaceScope, batch_id: str) -> tuple[list[str], int]:
    """The batch's runs, and how many of them have been judged."""
    rows = (
        await scope.session.execute(
            select(models.JudgeBatchRun.run_id, models.JudgeBatchRun.judged_at).where(
                models.JudgeBatchRun.workspace_id == scope.workspace_id,
                models.JudgeBatchRun.judge_batch_id == batch_id,
            )
        )
    ).all()
    return [r for r, _ in rows], sum(judged is not None for _, judged in rows)


async def due_schedules(session: AsyncSession, limit: int) -> list[models.JudgeSchedule]:
    """Schedules whose time has come, in any workspace, locked so that two workers never
    start the same one. The worker then scopes everything by each one's workspace."""
    return list(
        await session.scalars(
            select(models.JudgeSchedule)
            .where(
                models.JudgeSchedule.enabled.is_(True),
                models.JudgeSchedule.next_run_at <= func.now(),
            )
            .order_by(models.JudgeSchedule.next_run_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )


async def schedule_of(scope: WorkspaceScope, agent_id: str) -> models.JudgeSchedule | None:
    return await scope.first(
        scope.query(models.JudgeSchedule).where(models.JudgeSchedule.agent_id == agent_id)
    )


async def live_runs_of_version(scope: WorkspaceScope, version_id: str) -> int:
    count = await scope.session.scalar(
        select(func.count())
        .select_from(models.Run)
        .where(
            models.Run.workspace_id == scope.workspace_id,
            models.Run.agent_version_id == version_id,
            models.Run.experiment_id.is_(None),
        )
    )
    return int(count or 0)


async def new_version_batch(scope: WorkspaceScope, version_id: str) -> models.JudgeBatch | None:
    return await scope.first(
        scope.query(models.JudgeBatch).where(
            models.JudgeBatch.agent_version_id == version_id,
            models.JudgeBatch.trigger == models.JudgeBatchTrigger.NEW_VERSION,
        )
    )
