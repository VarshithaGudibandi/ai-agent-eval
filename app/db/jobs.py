import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.db.ids import new_id
from app.db.scope import WorkspaceScope

# The job queue. It is shared by every workspace: one worker serves them all. Claiming and
# reconciling are the only reads across workspaces, and every row they touch carries its
# workspace id, which the job's handler then scopes everything by.

PENDING = (models.JobStatus.QUEUED, models.JobStatus.RUNNING)


class LeaseLostError(Exception):
    """The job's lease expired, moved to another worker, or was revoked.

    A worker holding a stale lease gets this on its next fenced write, which changes nothing.
    """

    code = "lease_lost"


@dataclass(frozen=True)
class Claim:
    workspace_id: str
    job_id: str
    kind: str
    subject_id: str
    payload: dict[str, Any]
    lease_token: str
    attempt: int


def _lease_until(seconds: float) -> ColumnElement[Any]:
    # Database time, so every worker and the reconciler agree on when a lease ends.
    return func.now() + timedelta(seconds=seconds)


def lease_holds(job_id: str, lease_token: str) -> ColumnElement[bool]:
    """The condition every fenced write checks, in the same transaction as the write."""
    return and_(
        models.Job.id == job_id,
        models.Job.lease_token == lease_token,
        models.Job.status == models.JobStatus.RUNNING,
        models.Job.lease_expires_at > func.now(),
    )


async def enqueue(
    scope: WorkspaceScope, kind: str, subject_id: str, payload: dict[str, Any] | None = None
) -> str | None:
    """Queue work. Returns the new job's id, or None if the same work is already pending."""
    job_id = new_id("job")
    inserted = await scope.session.scalar(
        insert(models.Job)
        .values(
            id=job_id,
            workspace_id=scope.workspace_id,
            kind=kind,
            subject_id=subject_id,
            payload=payload or {},
            status=models.JobStatus.QUEUED,
        )
        .on_conflict_do_nothing(
            index_elements=["workspace_id", "kind", "subject_id"],
            # Must repeat the partial index's predicate exactly for Postgres to match it.
            index_where=text("status IN ('queued', 'running')"),
        )
        .returning(models.Job.id)
    )
    return inserted


async def claim_next(
    session: AsyncSession,
    worker_id: str,
    lease_seconds: float,
    kinds: list[str],
    workspace_id: str | None = None,
) -> Claim | None:
    """Take the oldest queued job of a kind this worker handles, holding a fresh lease.
    Given a workspace, only that workspace's jobs.

    SKIP LOCKED lets many workers claim at once without ever taking the same job.
    """
    conditions = [
        models.Job.status == models.JobStatus.QUEUED,
        models.Job.available_at <= func.now(),
        models.Job.kind.in_(kinds),
    ]
    if workspace_id is not None:
        conditions.append(models.Job.workspace_id == workspace_id)
    job = await session.scalar(
        select(models.Job)
        .where(*conditions)
        .order_by(models.Job.available_at, models.Job.created_at, models.Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    token = secrets.token_urlsafe(24)
    job.status = models.JobStatus.RUNNING
    job.attempts += 1
    job.lease_token = token
    job.lease_expires_at = _lease_until(lease_seconds)
    job.worker_id = worker_id
    return Claim(
        workspace_id=job.workspace_id,
        job_id=job.id,
        kind=job.kind,
        subject_id=job.subject_id,
        payload=job.payload,
        lease_token=token,
        attempt=job.attempts,
    )


async def renew_lease(
    session: AsyncSession, workspace_id: str, job_id: str, token: str, lease_seconds: float
) -> bool:
    """Extend a live lease. False means it is lost, expired or revoked: stop working."""
    renewed = await session.scalar(
        update(models.Job)
        .where(models.Job.workspace_id == workspace_id, lease_holds(job_id, token))
        .values(lease_expires_at=_lease_until(lease_seconds))
        .returning(models.Job.id)
    )
    return renewed is not None


async def require_lease(
    session: AsyncSession, workspace_id: str, job_id: str, lease_token: str
) -> None:
    """Lock the job for this transaction if its lease still holds, or refuse."""
    held = await session.scalar(
        select(models.Job.id)
        .where(models.Job.workspace_id == workspace_id, lease_holds(job_id, lease_token))
        .with_for_update()
    )
    if held is None:
        raise LeaseLostError(job_id)


async def _close(
    session: AsyncSession,
    workspace_id: str,
    job_id: str,
    token: str,
    status: models.JobStatus,
    error: str | None,
) -> bool:
    closed = await session.scalar(
        update(models.Job)
        .where(models.Job.workspace_id == workspace_id, lease_holds(job_id, token))
        .values(status=status, lease_token=None, last_error=error, finished_at=func.now())
        .returning(models.Job.id)
    )
    return closed is not None


async def finish(session: AsyncSession, workspace_id: str, job_id: str, token: str) -> bool:
    """Mark the job done. False means the lease was lost first, so nothing is recorded."""
    return await _close(session, workspace_id, job_id, token, models.JobStatus.DONE, None)


async def fail(
    session: AsyncSession, workspace_id: str, job_id: str, token: str, error: str
) -> bool:
    """Mark the job failed for good. Work that raises would only raise again on a retry."""
    return await _close(session, workspace_id, job_id, token, models.JobStatus.FAILED, error[:500])


async def reconcile_expired(session: AsyncSession, max_attempts: int) -> list[str]:
    """Give jobs whose lease ran out another go, or give up on them after max_attempts.

    A lease runs out when a worker crashed or was killed, which says nothing about the work.
    """
    expired = list(
        await session.scalars(
            select(models.Job)
            .where(
                models.Job.status == models.JobStatus.RUNNING,
                models.Job.lease_expires_at < func.now(),
            )
            .with_for_update(skip_locked=True)
        )
    )
    for job in expired:
        job.lease_token = None
        if job.attempts < max_attempts:
            job.status = models.JobStatus.QUEUED
            job.available_at = func.now()
        else:
            job.status = models.JobStatus.FAILED
            job.last_error = f"gave up after {job.attempts} interrupted attempts"
            job.finished_at = func.now()
    return [j.id for j in expired]


async def cancel(scope: WorkspaceScope, subject_id: str) -> int:
    """Cancel pending work on a subject. Revoking a running job's lease is what stops it.

    Returns how many running jobs lost their lease.
    """
    jobs = await scope.all(
        scope.query(models.Job)
        .where(models.Job.subject_id == subject_id, models.Job.status.in_(PENDING))
        .with_for_update()
    )
    running = 0
    for job in jobs:
        running += job.status == models.JobStatus.RUNNING
        job.status = models.JobStatus.CANCELLED
        job.lease_token = None
        job.finished_at = func.now()
    return running
