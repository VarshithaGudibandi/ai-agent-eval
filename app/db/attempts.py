from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.db.scope import WorkspaceScope

LIVE = (models.ExecutionStatus.QUEUED, models.ExecutionStatus.RUNNING)


class AttemptNotLiveError(Exception):
    """The attempt was interrupted, cancelled or finished, or its lease ran out or moved.

    A worker holding a stale lease gets this on its next step, and that step changes nothing.
    """

    code = "attempt_not_live"


def lease_holds(attempt_id: str, lease_token: str) -> ColumnElement[bool]:
    """The condition every state-changing step checks, in the same transaction as its write."""
    return and_(
        models.Attempt.id == attempt_id,
        models.Attempt.lease_token == lease_token,
        models.Attempt.execution_status.in_(LIVE),
        models.Attempt.lease_expires_at > func.now(),
    )


async def require_live(
    session: AsyncSession, workspace_id: str, attempt_id: str, lease_token: str
) -> None:
    """Lock the attempt for this transaction if the lease still holds, or refuse."""
    held = await session.scalar(
        select(models.Attempt.id)
        .where(models.Attempt.workspace_id == workspace_id, lease_holds(attempt_id, lease_token))
        .with_for_update()
    )
    if held is None:
        raise AttemptNotLiveError(attempt_id)


@dataclass
class AttemptRows:
    attempt: models.Attempt
    run: models.Run
    task: models.TaskVersion
    config: models.AgentConfig
    experiment: models.Experiment


async def load_attempt(scope: WorkspaceScope, attempt_id: str) -> AttemptRows:
    attempt = await scope.require(models.Attempt, attempt_id)
    run = await scope.require(models.Run, attempt.run_id)
    return AttemptRows(
        attempt=attempt,
        run=run,
        task=await scope.require(models.TaskVersion, run.task_version_id),
        config=await scope.require(models.AgentConfig, run.agent_config_id),
        experiment=await scope.require(models.Experiment, run.experiment_id),
    )


async def trace_events(scope: WorkspaceScope, attempt_id: str) -> list[models.TraceEvent]:
    return await scope.all(
        scope.query(models.TraceEvent)
        .where(models.TraceEvent.attempt_id == attempt_id)
        .order_by(models.TraceEvent.sequence_number)
    )


async def attempts_for_run(scope: WorkspaceScope, run_id: str) -> list[models.Attempt]:
    return await scope.all(
        scope.query(models.Attempt)
        .where(models.Attempt.run_id == run_id)
        .order_by(models.Attempt.attempt_number)
    )


async def scores_for_attempt(scope: WorkspaceScope, attempt_id: str) -> list[models.Score]:
    return await scope.all(
        scope.query(models.Score)
        .where(models.Score.attempt_id == attempt_id)
        .order_by(models.Score.evaluator_name, models.Score.dimension)
    )


async def snapshots_for_attempt(
    scope: WorkspaceScope, attempt_id: str
) -> dict[str, dict[str, Any]]:
    """The initial and final environment state of an attempt, by kind."""
    rows = await scope.all(
        scope.query(models.EnvSnapshot).where(models.EnvSnapshot.attempt_id == attempt_id)
    )
    return {row.kind: row.state for row in rows}
