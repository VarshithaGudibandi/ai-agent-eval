from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db import models
from app.db.budgets import BudgetLimits, create_budget
from app.db.ids import new_id
from app.db.scope import WorkspaceScope


async def pause_for_budget(scope: WorkspaceScope, experiment_id: str) -> bool:
    """Pause a running experiment. Returns True only if this call paused it."""
    experiment = await scope.session.scalar(
        scope.query(models.Experiment)
        .where(models.Experiment.id == experiment_id)
        .with_for_update()
    )
    if experiment is None or experiment.status != models.ExperimentStatus.RUNNING:
        return False
    experiment.status = models.ExperimentStatus.PAUSED_BUDGET
    return True


async def experiments_of(
    scope: WorkspaceScope, agent_id: str, limit: int
) -> list[models.Experiment]:
    """The agent's experiments, newest first."""
    return await scope.all(
        scope.query(models.Experiment)
        .join(models.AgentVersion, models.AgentVersion.id == models.Experiment.agent_version_id)
        .where(models.AgentVersion.agent_id == agent_id)
        .order_by(models.Experiment.created_at.desc(), models.Experiment.id.desc())
        .limit(limit)
    )


class IdempotencyKeyReusedError(ValueError):
    """The key already started a different experiment."""


async def _replay(
    scope: WorkspaceScope,
    version: models.AgentVersion,
    dataset_version_id: str,
    repetitions: int,
    idempotency_key: str,
) -> models.Experiment | None:
    existing = await scope.first(
        scope.query(models.Experiment).where(models.Experiment.idempotency_key == idempotency_key)
    )
    if existing is None:
        return None
    same = (
        existing.agent_version_id == version.id
        and existing.dataset_version_id == dataset_version_id
        and existing.repetitions == repetitions
    )
    if not same:
        raise IdempotencyKeyReusedError(idempotency_key)
    return existing


async def start_experiment(
    scope: WorkspaceScope,
    version: models.AgentVersion,
    dataset_version_id: str,
    repetitions: int,
    idempotency_key: str | None,
) -> tuple[models.Experiment, bool]:
    """Start an experiment with its budget. Returns it and whether it is new.

    A retried start with the same key returns the first experiment, never a second one.
    """
    if idempotency_key is not None:
        existing = await _replay(scope, version, dataset_version_id, repetitions, idempotency_key)
        if existing is not None:
            return existing, False
    dataset_version = await scope.require(models.DatasetVersion, dataset_version_id)
    experiment = models.Experiment(
        id=new_id("exp"),
        workspace_id=scope.workspace_id,
        agent_version_id=version.id,
        dataset_version_id=dataset_version.id,
        status=models.ExperimentStatus.RUNNING,
        repetitions=repetitions,
        idempotency_key=idempotency_key,
    )
    try:
        async with scope.session.begin_nested():
            scope.add(experiment)
            await scope.flush()
    except IntegrityError:
        # A retry that overlapped the first request lost the race for the key.
        if idempotency_key is None:
            raise
        winner = await _replay(scope, version, dataset_version_id, repetitions, idempotency_key)
        if winner is None:
            raise
        return winner, False
    await create_budget(scope, experiment.id, BudgetLimits())
    return experiment, True


async def finish_experiment(scope: WorkspaceScope, experiment_id: str) -> models.Experiment:
    """Mark that every trace has been sent. Finishing twice changes nothing."""
    experiment = await scope.require(models.Experiment, experiment_id)
    if experiment.status == models.ExperimentStatus.RUNNING:
        experiment.status = models.ExperimentStatus.FINISHED
        experiment.finished_at = func.now()
    return experiment


async def runs_received(scope: WorkspaceScope, experiment_id: str) -> int:
    count = await scope.session.scalar(
        select(func.count())
        .select_from(models.Run)
        .where(
            models.Run.workspace_id == scope.workspace_id,
            models.Run.experiment_id == experiment_id,
        )
    )
    return int(count or 0)


async def case_by_key(
    scope: WorkspaceScope, experiment: models.Experiment, case_key: str
) -> models.DatasetCase | None:
    return await scope.first(
        scope.query(models.DatasetCase).where(
            models.DatasetCase.dataset_version_id == experiment.dataset_version_id,
            models.DatasetCase.case_key == case_key,
        )
    )


async def has_run(scope: WorkspaceScope, experiment_id: str, case_id: str, repetition: int) -> bool:
    found = await scope.first(
        scope.query(models.Run).where(
            models.Run.experiment_id == experiment_id,
            models.Run.case_id == case_id,
            models.Run.repetition == repetition,
        )
    )
    return found is not None
