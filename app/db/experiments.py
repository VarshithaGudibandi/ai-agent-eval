from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.db.ids import new_id
from app.evaluators.contract import Evaluator


async def create_experiment(
    session: AsyncSession,
    workspace_id: str,
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
        existing = await session.scalar(
            select(models.Experiment).where(
                models.Experiment.workspace_id == workspace_id,
                models.Experiment.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing
    version = await session.scalar(
        select(models.DatasetVersion).where(
            models.DatasetVersion.workspace_id == workspace_id,
            models.DatasetVersion.id == dataset_version_id,
        )
    )
    if version is None:
        raise LookupError(dataset_version_id)
    tasks = list(
        await session.scalars(
            select(models.TaskVersion)
            .where(
                models.TaskVersion.workspace_id == workspace_id,
                models.TaskVersion.dataset_version_id == dataset_version_id,
            )
            .order_by(models.TaskVersion.task_key)
        )
    )
    configs = {
        c.id: c
        for c in await session.scalars(
            select(models.AgentConfig).where(
                models.AgentConfig.workspace_id == workspace_id,
                models.AgentConfig.id.in_(agent_config_ids),
            )
        )
    }
    missing = set(agent_config_ids) - set(configs)
    if missing:
        raise LookupError(sorted(missing))
    experiment = models.Experiment(
        id=new_id("exp"),
        workspace_id=workspace_id,
        dataset_version_id=dataset_version_id,
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
    session.add(experiment)
    await session.flush()
    for repetition in range(1, repetitions + 1):
        for task in tasks:
            for config_id in agent_config_ids:
                session.add(
                    models.Run(
                        id=new_id("run"),
                        workspace_id=workspace_id,
                        experiment_id=experiment.id,
                        agent_config_id=config_id,
                        task_version_id=task.id,
                        repetition=repetition,
                        execution_status=models.ExecutionStatus.QUEUED,
                        evaluation_status=models.EvaluationStatus.PENDING,
                    )
                )
    await session.flush()
    return experiment


async def start_attempt(session: AsyncSession, workspace_id: str, run_id: str) -> models.Attempt:
    """Create the next attempt for a run. Each attempt gets a fresh environment."""
    latest = await session.scalar(
        select(func.max(models.Attempt.attempt_number)).where(
            models.Attempt.workspace_id == workspace_id, models.Attempt.run_id == run_id
        )
    )
    attempt = models.Attempt(
        id=new_id("att"),
        workspace_id=workspace_id,
        run_id=run_id,
        attempt_number=(latest or 0) + 1,
        env_id=new_id("env"),
        execution_status=models.ExecutionStatus.QUEUED,
    )
    session.add(attempt)
    await session.flush()
    return attempt
