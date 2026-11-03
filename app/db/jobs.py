from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.db.experiments import complete_if_done, start_attempt
from app.db.queue import finish_job, lease_until, requeue_jobs
from app.db.scope import WorkspaceScope

# The queue is shared by every workspace: a worker serves them all. These functions are the
# only place that reads across workspaces, and each row they touch carries its workspace id.

CLAIMABLE_EXPERIMENTS = (models.ExperimentStatus.QUEUED, models.ExperimentStatus.RUNNING)
LIVE_ATTEMPTS = (models.ExecutionStatus.QUEUED, models.ExecutionStatus.RUNNING)


@dataclass(frozen=True)
class Claim:
    workspace_id: str
    job_id: str
    run_id: str
    attempt_id: str
    lease_token: str


async def claim_next(
    session: AsyncSession,
    worker_id: str,
    lease_seconds: float,
    *,
    experiment_id: str | None = None,
) -> Claim | None:
    """Take the next queued job and start an attempt holding a fresh lease.

    SKIP LOCKED lets many workers claim at once without ever taking the same job.
    """
    query = (
        select(models.Job)
        .join(
            models.Experiment,
            (models.Experiment.id == models.Job.experiment_id)
            & (models.Experiment.workspace_id == models.Job.workspace_id),
        )
        .where(
            models.Job.status == models.JobStatus.QUEUED,
            models.Job.available_at <= func.now(),
            models.Experiment.status.in_(CLAIMABLE_EXPERIMENTS),
        )
        .order_by(models.Job.created_at, models.Job.position, models.Job.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=models.Job)
    )
    if experiment_id is not None:
        query = query.where(models.Job.experiment_id == experiment_id)
    job = await session.scalar(query)
    if job is None:
        return None
    job.status = models.JobStatus.RUNNING
    scope = WorkspaceScope(session, job.workspace_id)
    attempt = await start_attempt(scope, job.run_id, lease_seconds=lease_seconds)
    attempt.worker_id = worker_id
    return Claim(job.workspace_id, job.id, job.run_id, attempt.id, str(attempt.lease_token))


async def renew_lease(
    session: AsyncSession, workspace_id: str, attempt_id: str, token: str, lease_seconds: float
) -> bool:
    """Extend a live lease. False means it is lost, expired or revoked: stop working."""
    renewed = await session.scalar(
        update(models.Attempt)
        .where(
            models.Attempt.workspace_id == workspace_id,
            models.Attempt.id == attempt_id,
            models.Attempt.lease_token == token,
            models.Attempt.execution_status.in_(LIVE_ATTEMPTS),
            models.Attempt.lease_expires_at > func.now(),
        )
        .values(lease_expires_at=lease_until(lease_seconds))
        .returning(models.Attempt.id)
    )
    return renewed is not None


async def reconcile_expired(session: AsyncSession, max_attempts: int) -> list[str]:
    """Mark attempts whose lease ran out as interrupted and give their runs another go.

    The interrupted attempt's environment is left as it was, as evidence. A retry always
    gets a new attempt with a fresh environment. After max_attempts the run is given up as
    an infrastructure error, since repeated crashes say nothing about the agent.
    """
    expired = list(
        await session.scalars(
            select(models.Attempt)
            .where(
                models.Attempt.execution_status.in_(LIVE_ATTEMPTS),
                models.Attempt.lease_expires_at < func.now(),
            )
            .with_for_update(skip_locked=True)
        )
    )
    for attempt in expired:
        scope = WorkspaceScope(session, attempt.workspace_id)
        attempt.execution_status = models.ExecutionStatus.INTERRUPTED
        attempt.lease_token = None
        attempt.finished_at = func.now()
        run = await scope.require(models.Run, attempt.run_id)
        if attempt.attempt_number < max_attempts:
            run.execution_status = models.ExecutionStatus.QUEUED
            await requeue_jobs(scope, [run.id])
        else:
            run.execution_status = models.ExecutionStatus.INFRASTRUCTURE_ERROR
            await finish_job(scope, run.id)
            await scope.flush()
            await complete_if_done(scope, run.experiment_id)
    return [a.id for a in expired]


CANCELLABLE = {
    models.ExperimentStatus.QUEUED,
    models.ExperimentStatus.RUNNING,
    models.ExperimentStatus.PAUSED_BUDGET,
    models.ExperimentStatus.PAUSED_USER,
}
UNFINISHED_RUNS = (
    models.ExecutionStatus.QUEUED,
    models.ExecutionStatus.RUNNING,
    models.ExecutionStatus.BUDGET_EXCEEDED,
)


async def cancel_work(scope: WorkspaceScope, experiment_id: str) -> int:
    """Cancel every run not yet finished. In-flight attempts lose their lease right away.

    Revoking the lease is what stops a running agent: its next tool call, model call or
    trace write is refused. Returns how many attempts were in flight.
    """
    await scope.session.execute(
        update(models.Job)
        .where(
            models.Job.workspace_id == scope.workspace_id,
            models.Job.experiment_id == experiment_id,
            models.Job.status.in_((models.JobStatus.QUEUED, models.JobStatus.RUNNING)),
        )
        .values(status=models.JobStatus.CANCELLED)
    )
    runs = await scope.all(
        scope.query(models.Run).where(
            models.Run.experiment_id == experiment_id,
            models.Run.execution_status.in_(UNFINISHED_RUNS),
        )
    )
    for run in runs:
        run.execution_status = models.ExecutionStatus.CANCELLED
    in_flight = await scope.all(
        scope.query(models.Attempt)
        .where(
            models.Attempt.run_id.in_([r.id for r in runs]),
            models.Attempt.execution_status.in_(LIVE_ATTEMPTS),
        )
        .with_for_update()
    )
    for attempt in in_flight:
        attempt.execution_status = models.ExecutionStatus.CANCELLED
        attempt.lease_token = None
        attempt.finished_at = func.now()
    return len(in_flight)
