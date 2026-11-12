import asyncio

import pytest
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.ids import new_id
from app.db.jobs import (
    Claim,
    LeaseLostError,
    cancel,
    claim_next,
    enqueue,
    fail,
    finish,
    reconcile_expired,
    renew_lease,
    require_lease,
)
from app.db.scope import WorkspaceScope

Sessions = async_sessionmaker[AsyncSession]
KIND = "queue_test"


async def _enqueue(sessionmaker: Sessions, workspace_id: str, subject: str = "trace_1") -> str:
    async with sessionmaker.begin() as session:
        job_id = await enqueue(WorkspaceScope(session, workspace_id), KIND, subject, {"n": 1})
    assert job_id is not None
    return job_id


async def _claim(sessionmaker: Sessions, worker: str = "w1") -> Claim | None:
    async with sessionmaker.begin() as session:
        return await claim_next(session, worker, 30, [KIND])


async def _expire(sessionmaker: Sessions, job_id: str) -> None:
    async with sessionmaker.begin() as session:
        await session.execute(
            update(models.Job)
            .where(models.Job.id == job_id)
            .values(lease_expires_at=func.now() - func.make_interval(0, 0, 0, 0, 0, 0, 1))
        )


async def _job(sessionmaker: Sessions, job_id: str) -> models.Job:
    async with sessionmaker() as session:
        return await session.get_one(models.Job, job_id)


@pytest.fixture(autouse=True)
async def empty_queue(sessionmaker: Sessions) -> None:
    # The queue is shared by every workspace, so leftovers from other tests would be claimed.
    async with sessionmaker.begin() as session:
        await session.execute(
            update(models.Job)
            .where(models.Job.status.in_(("queued", "running")))
            .values(status="cancelled", lease_token=None)
        )


async def test_queueing_the_same_pending_work_twice_keeps_one_job(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    first = await _enqueue(sessionmaker, workspace_id)
    async with sessionmaker.begin() as session:
        again = await enqueue(WorkspaceScope(session, workspace_id), KIND, "trace_1")
    assert again is None
    assert (await _job(sessionmaker, first)).payload == {"n": 1}


async def test_claim_holds_a_lease_and_counts_the_attempt(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    job_id = await _enqueue(sessionmaker, workspace_id)
    claim = await _claim(sessionmaker)
    assert claim is not None
    assert (claim.job_id, claim.kind, claim.subject_id, claim.attempt) == (
        job_id,
        KIND,
        "trace_1",
        1,
    )
    job = await _job(sessionmaker, job_id)
    assert (job.status, job.worker_id, job.lease_token) == ("running", "w1", claim.lease_token)
    assert await _claim(sessionmaker) is None


async def test_workers_claim_only_kinds_they_handle(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    await _enqueue(sessionmaker, workspace_id)
    async with sessionmaker.begin() as session:
        assert await claim_next(session, "w1", 30, ["explain"]) is None


async def test_concurrent_workers_never_claim_the_same_job(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    for n in range(5):
        await _enqueue(sessionmaker, workspace_id, f"trace_{n}")
    claims = await asyncio.gather(*(_claim(sessionmaker, f"w{n}") for n in range(8)))
    taken = [c.job_id for c in claims if c is not None]
    assert len(taken) == len(set(taken)) == 5


async def test_a_lease_renews_only_for_its_holder_while_live(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    await _enqueue(sessionmaker, workspace_id)
    claim = await _claim(sessionmaker)
    assert claim is not None
    async with sessionmaker.begin() as session:
        assert await renew_lease(session, workspace_id, claim.job_id, claim.lease_token, 30)
        assert not await renew_lease(session, workspace_id, claim.job_id, "someone-else", 30)
    await _expire(sessionmaker, claim.job_id)
    async with sessionmaker.begin() as session:
        assert not await renew_lease(session, workspace_id, claim.job_id, claim.lease_token, 30)
        with pytest.raises(LeaseLostError):
            await require_lease(session, workspace_id, claim.job_id, claim.lease_token)


async def test_an_expired_job_is_retried_then_given_up(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    job_id = await _enqueue(sessionmaker, workspace_id)
    for attempt in (1, 2, 3):
        claim = await _claim(sessionmaker)
        assert claim is not None and claim.attempt == attempt
        await _expire(sessionmaker, job_id)
        async with sessionmaker.begin() as session:
            assert await reconcile_expired(session, max_attempts=3) == [job_id]
    job = await _job(sessionmaker, job_id)
    assert (job.status, job.lease_token) == ("failed", None)
    assert job.last_error == "gave up after 3 interrupted attempts"
    assert await _claim(sessionmaker) is None


async def test_only_the_lease_holder_can_finish_or_fail(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    job_id = await _enqueue(sessionmaker, workspace_id)
    claim = await _claim(sessionmaker)
    assert claim is not None
    async with sessionmaker.begin() as session:
        assert not await finish(session, workspace_id, job_id, "stale-token")
        assert not await fail(session, workspace_id, job_id, "stale-token", "boom")
        assert await finish(session, workspace_id, job_id, claim.lease_token)
    job = await _job(sessionmaker, job_id)
    assert (job.status, job.lease_token) == ("done", None)
    assert job.finished_at is not None


async def test_cancelling_revokes_running_leases_and_drops_queued_work(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    running = await _enqueue(sessionmaker, workspace_id)
    claim = await _claim(sessionmaker)
    assert claim is not None
    async with sessionmaker.begin() as session:
        queued = await enqueue(WorkspaceScope(session, workspace_id), "explain", "trace_1")
    async with sessionmaker.begin() as session:
        assert await cancel(WorkspaceScope(session, workspace_id), "trace_1") == 1
    assert (await _job(sessionmaker, running)).status == "cancelled"
    assert queued is not None and (await _job(sessionmaker, queued)).status == "cancelled"
    async with sessionmaker.begin() as session:
        assert not await renew_lease(session, workspace_id, running, claim.lease_token, 30)
        assert not await finish(session, workspace_id, running, claim.lease_token)


async def test_cancel_touches_only_its_own_workspace(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    other = models.Workspace(id=new_id("ws"), name="other")
    async with sessionmaker.begin() as session:
        session.add(other)
    theirs = await _enqueue(sessionmaker, other.id)
    async with sessionmaker.begin() as session:
        assert await cancel(WorkspaceScope(session, workspace_id), "trace_1") == 0
    assert (await _job(sessionmaker, theirs)).status == "queued"
