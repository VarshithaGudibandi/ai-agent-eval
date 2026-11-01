from dataclasses import dataclass

from app.db import models
from app.db.scope import WorkspaceScope


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
