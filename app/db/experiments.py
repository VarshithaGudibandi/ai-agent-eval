from collections.abc import Sequence

from sqlalchemy import func, select

from app.db import models
from app.db.ids import new_id
from app.db.scope import NotFoundError, WorkspaceScope
from app.evaluators.contract import Evaluator


class ExperimentRejectedError(ValueError):
    pass


async def create_experiment(
    scope: WorkspaceScope,
    dataset_version_id: str,
    agent_config_ids: Sequence[str],
    evaluators: Sequence[Evaluator],
    *,
    mode: models.ExperimentMode,
    repetitions: int = 1,
    idempotency_key: str | None = None,
) -> models.Experiment:
    """Create the experiment and all its runs together, or return the one already made."""
    if idempotency_key is not None:
        existing = await scope.first(
            scope.query(models.Experiment).where(
                models.Experiment.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing
    version = await scope.require(models.DatasetVersion, dataset_version_id)
    tasks = await scope.all(
        scope.query(models.TaskVersion)
        .where(models.TaskVersion.dataset_version_id == version.id)
        .order_by(models.TaskVersion.task_key)
    )
    configs = {
        c.id: c
        for c in await scope.all(
            scope.query(models.AgentConfig).where(models.AgentConfig.id.in_(agent_config_ids))
        )
    }
    missing = sorted(set(agent_config_ids) - set(configs))
    if missing:
        raise NotFoundError(f"agent_configs:{','.join(missing)}")
    scripted = [c.id for c in configs.values() if c.adapter_type == models.AdapterType.SCRIPTED]
    if scripted and mode == models.ExperimentMode.REAL:
        # Scripted agents are canned answers. Letting them into a real experiment would
        # produce numbers that look like a real result.
        raise ExperimentRejectedError("scripted agents can only run in demo experiments")
    experiment = models.Experiment(
        id=new_id("exp"),
        workspace_id=scope.workspace_id,
        dataset_version_id=version.id,
        mode=mode,
        status=models.ExperimentStatus.QUEUED,
        repetitions=repetitions,
        idempotency_key=idempotency_key,
        manifest={
            "dataset_version": {"id": version.id, "content_hash": version.content_hash},
            "environments": sorted({t.environment for t in tasks}),
            "agent_configs": [
                {"id": c_id, "config_hash": configs[c_id].config_hash} for c_id in agent_config_ids
            ],
            "evaluators": sorted(
                ({"name": e.name, "version": e.version} for e in evaluators),
                key=lambda e: (e["name"], e["version"]),
            ),
            "repetitions": repetitions,
        },
    )
    scope.add(experiment)
    await scope.flush()
    for repetition in range(1, repetitions + 1):
        for task in tasks:
            for config_id in agent_config_ids:
                scope.add(
                    models.Run(
                        id=new_id("run"),
                        workspace_id=scope.workspace_id,
                        experiment_id=experiment.id,
                        agent_config_id=config_id,
                        task_version_id=task.id,
                        repetition=repetition,
                        execution_status=models.ExecutionStatus.QUEUED,
                        evaluation_status=models.EvaluationStatus.PENDING,
                    )
                )
    await scope.flush()
    return experiment


async def start_attempt(scope: WorkspaceScope, run_id: str) -> models.Attempt:
    """Create the next attempt for a run. Each attempt gets a fresh environment."""
    run = await scope.require(models.Run, run_id)
    latest = await scope.session.scalar(
        select(func.max(models.Attempt.attempt_number)).where(
            models.Attempt.workspace_id == scope.workspace_id, models.Attempt.run_id == run.id
        )
    )
    attempt = models.Attempt(
        id=new_id("att"),
        workspace_id=scope.workspace_id,
        run_id=run.id,
        attempt_number=(latest or 0) + 1,
        env_id=new_id("env"),
        execution_status=models.ExecutionStatus.QUEUED,
    )
    scope.add(attempt)
    await scope.flush()
    return attempt


async def complete_if_done(scope: WorkspaceScope, experiment_id: str) -> None:
    unfinished = await scope.first(
        scope.query(models.Run).where(
            models.Run.experiment_id == experiment_id,
            models.Run.execution_status.in_(
                [models.ExecutionStatus.QUEUED, models.ExecutionStatus.RUNNING]
            ),
        )
    )
    if unfinished is None:
        experiment = await scope.require(models.Experiment, experiment_id)
        experiment.status = models.ExperimentStatus.COMPLETED


async def find_by_idempotency_key(scope: WorkspaceScope, key: str) -> models.Experiment | None:
    return await scope.first(
        scope.query(models.Experiment).where(models.Experiment.idempotency_key == key)
    )


async def list_experiments(scope: WorkspaceScope) -> list[models.Experiment]:
    return await scope.all(
        scope.query(models.Experiment).order_by(
            models.Experiment.created_at.desc(), models.Experiment.id
        )
    )


async def run_counts(
    scope: WorkspaceScope, experiment_ids: Sequence[str]
) -> dict[str, dict[str, int]]:
    rows = await scope.session.execute(
        select(models.Run.experiment_id, models.Run.evaluation_status, func.count())
        .where(
            models.Run.workspace_id == scope.workspace_id,
            models.Run.experiment_id.in_(experiment_ids),
        )
        .group_by(models.Run.experiment_id, models.Run.evaluation_status)
    )
    counts: dict[str, dict[str, int]] = {}
    for experiment_id, status, count in rows:
        counts.setdefault(experiment_id, {})[status] = count
    return counts


async def list_runs(
    scope: WorkspaceScope,
    experiment_id: str,
    *,
    evaluation_status: str | None,
    limit: int,
    offset: int,
) -> list[models.Run]:
    experiment = await scope.require(models.Experiment, experiment_id)
    query = scope.query(models.Run).where(models.Run.experiment_id == experiment.id)
    if evaluation_status is not None:
        query = query.where(models.Run.evaluation_status == evaluation_status)
    return await scope.all(
        query.order_by(models.Run.repetition, models.Run.task_version_id, models.Run.id)
        .limit(limit)
        .offset(offset)
    )
