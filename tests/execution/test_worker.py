import asyncio
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.audit import record_audit
from app.db.ids import new_id
from app.db.jobs import LeaseLostError, claim_next, enqueue, require_lease
from app.db.scope import WorkspaceScope
from app.execution.worker import JobContext, Worker, run
from app.settings import Settings

Sessions = async_sessionmaker[AsyncSession]
ROOT = Path(__file__).parents[2]
KIND = "test_job"


def _fast(settings: Settings, database_url: str, **changes: object) -> Settings:
    return settings.model_copy(
        update={
            "database_url": database_url,
            "worker_idle_seconds": 0.05,
            "lease_seconds": 2.0,
            "heartbeat_seconds": 0.5,
            "reconcile_seconds": 0.2,
            **changes,
        }
    )


async def _workspace(sessionmaker: Sessions) -> str:
    workspace = models.Workspace(id=new_id("ws"), name="worker")
    async with sessionmaker.begin() as session:
        session.add(workspace)
    return workspace.id


async def _enqueue(sessionmaker: Sessions, workspace_id: str, *subjects: str) -> list[str]:
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        ids = [await enqueue(scope, KIND, s) for s in subjects]
    return [i for i in ids if i is not None]


async def record_result(ctx: JobContext, by: str) -> None:
    """The one write a job makes, fenced on its lease like every real result."""
    claim = ctx.claim
    async with ctx.sessionmaker.begin() as session:
        await require_lease(session, claim.workspace_id, claim.job_id, claim.lease_token)
        record_audit(
            WorkspaceScope(session, claim.workspace_id),
            models.ActorType.SYSTEM,
            by,
            "job.result",
            "job",
            claim.job_id,
        )


async def _results(sessionmaker: Sessions, workspace_id: str) -> list[tuple[str, str | None]]:
    async with sessionmaker() as session:
        rows = await session.execute(
            select(models.AuditEntry.actor_id, models.AuditEntry.target_id).where(
                models.AuditEntry.workspace_id == workspace_id,
                models.AuditEntry.action == "job.result",
            )
        )
        return [(actor, target) for actor, target in rows]


async def _statuses(sessionmaker: Sessions, workspace_id: str) -> dict[str, str]:
    async with sessionmaker() as session:
        jobs = await session.scalars(
            select(models.Job).where(models.Job.workspace_id == workspace_id)
        )
        return {j.subject_id: j.status for j in jobs}


async def _until(check: Callable[[], Awaitable[bool]], seconds: float = 15) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not await check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


async def _serve(worker: Worker, until: Callable[[], Awaitable[bool]]) -> None:
    stop = asyncio.Event()
    serving = asyncio.create_task(worker.run(stop))
    try:
        await _until(until)
    finally:
        stop.set()
        await asyncio.wait_for(serving, timeout=10)


async def test_worker_becomes_ready_and_stops_on_signal(
    settings: Settings, fresh_database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.execution.worker")
    stop = asyncio.Event()
    task = asyncio.create_task(run(_fast(settings, fresh_database_url), stop))
    await asyncio.sleep(0.5)
    assert not task.done(), "worker exited before being told to stop"
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    messages = [r.getMessage() for r in caplog.records]
    assert "worker ready" in messages
    assert messages[-1] == "worker stopped"


async def test_worker_waits_while_database_is_down(
    unreachable_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.execution.worker")
    stop = asyncio.Event()
    task = asyncio.create_task(run(unreachable_settings, stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    messages = [r.getMessage() for r in caplog.records]
    assert "waiting for database" in messages
    assert "worker ready" not in messages


async def test_a_worker_process_starts_and_stops_cleanly(fresh_database_url: str) -> None:
    env = {
        **os.environ,
        "DATABASE_URL": fresh_database_url,
        "WORKER_IDLE_SECONDS": "0.1",
        "LOG_FORMAT": "text",
    }
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.execution.worker",
        cwd=ROOT,
        env=env,
        stdout=asyncio.subprocess.PIPE,
    )
    assert worker.stdout is not None
    line = await asyncio.wait_for(worker.stdout.readline(), timeout=15)
    while b"worker ready" not in line:
        line = await asyncio.wait_for(worker.stdout.readline(), timeout=15)
    worker.send_signal(signal.SIGTERM)
    assert await asyncio.wait_for(worker.wait(), timeout=15) == 0


async def test_jobs_run_once_and_a_crashing_job_is_failed_with_its_reason(
    fresh_sessionmaker: Sessions, fresh_database_url: str, settings: Settings
) -> None:
    fast = _fast(settings, fresh_database_url)
    workspace_id = await _workspace(fresh_sessionmaker)
    await _enqueue(fresh_sessionmaker, workspace_id, "good-1", "good-2", "bad")

    async def handle(ctx: JobContext) -> None:
        if ctx.claim.subject_id == "bad":
            raise ValueError("the trace could not be read")
        await record_result(ctx, "worker")

    async def all_closed() -> bool:
        return set((await _statuses(fresh_sessionmaker, workspace_id)).values()) <= {
            "done",
            "failed",
        }

    await _serve(Worker(fresh_sessionmaker, fast, {KIND: handle}), all_closed)
    assert await _statuses(fresh_sessionmaker, workspace_id) == {
        "good-1": "done",
        "good-2": "done",
        "bad": "failed",
    }
    assert len(await _results(fresh_sessionmaker, workspace_id)) == 2
    async with fresh_sessionmaker() as session:
        bad = await session.scalar(select(models.Job).where(models.Job.subject_id == "bad"))
    assert bad is not None and "the trace could not be read" in str(bad.last_error)


async def test_a_worker_runs_a_bounded_number_of_jobs_at_once(
    fresh_sessionmaker: Sessions, fresh_database_url: str, settings: Settings
) -> None:
    fast = _fast(settings, fresh_database_url, worker_concurrency=2)
    workspace_id = await _workspace(fresh_sessionmaker)
    await _enqueue(fresh_sessionmaker, workspace_id, *(f"s{n}" for n in range(6)))
    running, most = 0, 0

    async def handle(ctx: JobContext) -> None:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.1)
        running -= 1
        await record_result(ctx, "worker")

    async def all_done() -> bool:
        return set((await _statuses(fresh_sessionmaker, workspace_id)).values()) == {"done"}

    await _serve(Worker(fresh_sessionmaker, fast, {KIND: handle}), all_done)
    assert most == 2


async def test_a_killed_worker_gets_one_clean_retry_and_its_late_result_is_refused(
    fresh_sessionmaker: Sessions, fresh_database_url: str, settings: Settings
) -> None:
    fast = _fast(settings, fresh_database_url)
    workspace_id = await _workspace(fresh_sessionmaker)
    [job_id] = await _enqueue(fresh_sessionmaker, workspace_id, "trace_1")

    # The first worker claims the job, then freezes and stops heartbeating: as good as killed.
    async with fresh_sessionmaker.begin() as session:
        claim = await claim_next(session, "frozen", 1.0, [KIND])
    assert claim is not None
    gate = asyncio.Event()

    async def frozen() -> None:
        await gate.wait()
        await record_result(JobContext(claim, fresh_sessionmaker), "frozen")

    stale = asyncio.create_task(frozen())

    # A healthy worker reconciles the expired lease and runs the job again from scratch.
    async def done() -> bool:
        return (await _statuses(fresh_sessionmaker, workspace_id)) == {"trace_1": "done"}

    healthy = Worker(
        fresh_sessionmaker,
        fast,
        {KIND: lambda ctx: record_result(ctx, "healthy")},
        worker_id="healthy",
    )
    await _serve(healthy, done)

    # The frozen worker wakes up. Its write is refused and changes nothing.
    gate.set()
    with pytest.raises(LeaseLostError):
        await asyncio.wait_for(stale, timeout=10)
    assert await _results(fresh_sessionmaker, workspace_id) == [("healthy", job_id)]
    async with fresh_sessionmaker() as session:
        job = await session.get_one(models.Job, job_id)
    assert (job.attempts, job.worker_id, job.lease_token) == (2, "healthy", None)
