from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select

from app.db import models
from app.db.budgets import BudgetLimits, create_budget
from app.db.ids import new_id
from app.db.queue import add_jobs, lease_until, new_lease_token, requeue_jobs
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
    split: models.Split = models.Split.DEV,
    final: bool = False,
    repetitions: int = 1,
    idempotency_key: str | None = None,
    budget: BudgetLimits | None = None,
    prices: dict[str, Any] | None = None,
) -> models.Experiment:
    """Create the experiment, its budget and its runs together, or return the existing one.

    It runs the tasks of one split. Held-out tasks run only in final experiments, and only
    reviewed tasks run outside dev.
    """
    if idempotency_key is not None:
        existing = await scope.first(
            scope.query(models.Experiment).where(
                models.Experiment.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing
    version = await scope.require(models.DatasetVersion, dataset_version_id)
    if split == models.Split.HELD_OUT and not final:
        raise ExperimentRejectedError("held-out tasks run only in final experiments")
    tasks = await scope.all(
        scope.query(models.TaskVersion)
        .where(
            models.TaskVersion.dataset_version_id == version.id,
            models.TaskVersion.split == split,
        )
        .order_by(models.TaskVersion.task_key)
    )
    if not tasks:
        raise ExperimentRejectedError(f"the dataset version has no {split} tasks")
    unreviewed = [t.task_key for t in tasks if t.reviewer is None]
    if split != models.Split.DEV and unreviewed:
        raise ExperimentRejectedError(
            f"unreviewed tasks run only in dev: {', '.join(unreviewed[:10])}"
            + (f" and {len(unreviewed) - 10} more" if len(unreviewed) > 10 else "")
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
    if mode == models.ExperimentMode.DEMO and len(scripted) != len(configs):
        # Demo mode needs no key and costs nothing, so it runs scripted agents only.
        raise ExperimentRejectedError("demo experiments can only run scripted agents")
    experiment = models.Experiment(
        id=new_id("exp"),
        workspace_id=scope.workspace_id,
        dataset_version_id=version.id,
        mode=mode,
        split=split,
        final=final,
        status=models.ExperimentStatus.QUEUED,
        repetitions=repetitions,
        idempotency_key=idempotency_key,
        manifest={
            "dataset_version": {"id": version.id, "content_hash": version.content_hash},
            "split": split,
            "final": final,
            "task_count": len(tasks),
            "environments": sorted({t.environment for t in tasks}),
            "agent_configs": [
                {"id": c_id, "config_hash": configs[c_id].config_hash} for c_id in agent_config_ids
            ],
            "evaluators": sorted(
                ({"name": e.name, "version": e.version} for e in evaluators),
                key=lambda e: (e["name"], e["version"]),
            ),
            "repetitions": repetitions,
            "prices": prices or {},
        },
    )
    scope.add(experiment)
    await scope.flush()
    await create_budget(scope, experiment.id, budget or BudgetLimits())
    runs: list[models.Run] = []
    for repetition in range(1, repetitions + 1):
        for task in tasks:
            for config_id in agent_config_ids:
                runs.append(
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
    for run in runs:
        scope.add(run)
    await scope.flush()
    # Runs are ordered repetition, task, config, so configs alternate in the queue.
    add_jobs(scope, experiment.id, runs)
    await scope.flush()
    return experiment


DEFAULT_LEASE_SECONDS = 30.0


async def start_attempt(
    scope: WorkspaceScope, run_id: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS
) -> models.Attempt:
    """Create the next attempt for a run, holding a fresh lease and a fresh environment."""
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
        lease_token=new_lease_token(),
        lease_expires_at=lease_until(lease_seconds),
    )
    scope.add(attempt)
    await scope.flush()
    await scope.session.refresh(attempt, ["lease_expires_at"])
    return attempt


UNFINISHED = (
    models.ExecutionStatus.QUEUED,
    models.ExecutionStatus.RUNNING,
    models.ExecutionStatus.BUDGET_EXCEEDED,
)


async def complete_if_done(scope: WorkspaceScope, experiment_id: str) -> None:
    """Mark a running experiment completed once no run is left to do. Paused ones stay paused."""
    experiment = await scope.require(models.Experiment, experiment_id)
    if experiment.status != models.ExperimentStatus.RUNNING:
        return
    unfinished = await scope.first(
        scope.query(models.Run).where(
            models.Run.experiment_id == experiment_id,
            models.Run.execution_status.in_(UNFINISHED),
        )
    )
    if unfinished is None:
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
    category: str | None = None,
    reason_code: str | None = None,
    limit: int,
    offset: int,
) -> list[tuple[models.Run, models.TaskVersion]]:
    experiment = await scope.require(models.Experiment, experiment_id)
    ws = scope.workspace_id
    query = (
        select(models.Run, models.TaskVersion)
        .join(
            models.TaskVersion,
            (models.TaskVersion.id == models.Run.task_version_id)
            & (models.TaskVersion.workspace_id == models.Run.workspace_id),
        )
        .where(models.Run.workspace_id == ws, models.Run.experiment_id == experiment.id)
    )
    if evaluation_status is not None:
        query = query.where(models.Run.evaluation_status == evaluation_status)
    if category is not None:
        query = query.where(models.TaskVersion.category == category)
    if reason_code is not None:
        failed_with = (
            select(models.Attempt.run_id)
            .join(
                models.Score,
                (models.Score.attempt_id == models.Attempt.id)
                & (models.Score.workspace_id == models.Attempt.workspace_id),
            )
            .where(
                models.Attempt.workspace_id == ws,
                models.Score.status == "fail",
                models.Score.reason_code == reason_code,
            )
        )
        query = query.where(models.Run.id.in_(failed_with))
    rows = await scope.session.execute(
        query.order_by(models.TaskVersion.task_key, models.Run.repetition, models.Run.id)
        .limit(limit)
        .offset(offset)
    )
    return [(run, task) for run, task in rows]


async def pause_for_budget(scope: WorkspaceScope, experiment_id: str) -> bool:
    """Pause a running experiment. Returns True only if this call paused it."""
    experiment = await scope.session.scalar(
        scope.query(models.Experiment)
        .where(models.Experiment.id == experiment_id)
        .with_for_update()
    )
    if experiment is None or experiment.status not in (
        models.ExperimentStatus.QUEUED,
        models.ExperimentStatus.RUNNING,
    ):
        return False
    experiment.status = models.ExperimentStatus.PAUSED_BUDGET
    return True


class InvalidTransitionError(ValueError):
    pass


async def transition(
    scope: WorkspaceScope,
    experiment_id: str,
    allowed_from: set[models.ExperimentStatus],
    to: models.ExperimentStatus,
) -> models.Experiment:
    experiment = await scope.session.scalar(
        scope.query(models.Experiment)
        .where(models.Experiment.id == experiment_id)
        .with_for_update()
    )
    if experiment is None:
        raise NotFoundError(f"experiments:{experiment_id}")
    if experiment.status not in allowed_from:
        raise InvalidTransitionError(f"cannot go from {experiment.status} to {to}")
    experiment.status = to
    return experiment


async def requeue_budget_stopped_runs(scope: WorkspaceScope, experiment_id: str) -> int:
    """Put runs that the budget stopped back in the queue. Each gets a fresh attempt."""
    runs = await scope.all(
        scope.query(models.Run).where(
            models.Run.experiment_id == experiment_id,
            models.Run.execution_status == models.ExecutionStatus.BUDGET_EXCEEDED,
        )
    )
    for run in runs:
        run.execution_status = models.ExecutionStatus.QUEUED
    await requeue_jobs(scope, [r.id for r in runs])
    return len(runs)
