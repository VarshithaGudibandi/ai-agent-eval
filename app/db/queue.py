import secrets
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import func

from app.db import models
from app.db.ids import new_id
from app.db.scope import WorkspaceScope

# Rows of the job queue. Claiming and recovering jobs lives in app.db.jobs.


def new_lease_token() -> str:
    return secrets.token_urlsafe(24)


def lease_until(seconds: float) -> object:
    # Database time, so every worker and the reconciler agree on when a lease ends.
    return func.now() + timedelta(seconds=seconds)


def add_jobs(scope: WorkspaceScope, experiment_id: str, runs: Sequence[models.Run]) -> None:
    """One job per run, in the order the runs are given."""
    for position, run in enumerate(runs, start=1):
        scope.add(
            models.Job(
                id=new_id("job"),
                workspace_id=scope.workspace_id,
                experiment_id=experiment_id,
                run_id=run.id,
                status=models.JobStatus.QUEUED,
                position=position,
            )
        )


async def finish_job(scope: WorkspaceScope, run_id: str) -> None:
    job = await scope.first(scope.query(models.Job).where(models.Job.run_id == run_id))
    if job is not None:
        job.status = models.JobStatus.DONE


async def requeue_jobs(scope: WorkspaceScope, run_ids: Sequence[str]) -> None:
    for job in await scope.all(scope.query(models.Job).where(models.Job.run_id.in_(run_ids))):
        job.status = models.JobStatus.QUEUED
