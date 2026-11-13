import asyncio
import logging
import signal
import socket
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.engine import create_engine, create_sessionmaker
from app.db.jobs import (
    Claim,
    LeaseLostError,
    claim_next,
    fail,
    finish,
    reconcile_expired,
    renew_lease,
)
from app.db.schema_status import schema_status
from app.jobs.leases import LeaseOutcome, hold_lease
from app.metrics.aggregate_job import AGGREGATE_DAY, aggregate_day_job
from app.scoring.job import SCORE_RUN, score_run_job
from app.scoring.judge_job import JUDGE_RUN, judge_run_job
from app.settings import Settings, get_settings
from app.structured_logging import configure_logging

logger = logging.getLogger(__name__)


async def wait_for_schema(engine: AsyncEngine, stop: asyncio.Event, retry_seconds: float) -> bool:
    # Claiming jobs against a half-migrated schema would corrupt state, so the
    # worker refuses to start until the database matches the code.
    while not stop.is_set():
        try:
            result = await schema_status(engine)
            if result.at_head:
                return True
            logger.info("waiting for migrations", extra={"current": result.current})
        except Exception as exc:
            logger.info("waiting for database", extra={"error": type(exc).__name__})
        try:
            await asyncio.wait_for(stop.wait(), timeout=retry_seconds)
        except TimeoutError:
            pass
    return False


@dataclass(frozen=True)
class JobContext:
    """What a job handler gets: the claimed job and a way to open transactions.

    A handler fences each write with app.db.jobs.require_lease on claim.lease_token, so a
    worker that lost the lease cannot record anything.
    """

    claim: Claim
    sessionmaker: async_sessionmaker[AsyncSession]
    settings: Settings = field(default_factory=get_settings)


Handler = Callable[[JobContext], Coroutine[Any, Any, None]]

# Job kinds this platform runs, by name. Stages add theirs as they are built.
HANDLERS: dict[str, Handler] = {
    SCORE_RUN: score_run_job,
    JUDGE_RUN: judge_run_job,
    AGGREGATE_DAY: aggregate_day_job,
}


class Worker:
    """Claims jobs from the Postgres queue and runs their handlers, a few at a time.

    Workers are stateless and share nothing but the database, so more of them simply means
    more throughput. Each also reconciles expired leases, so a crashed worker's jobs are
    picked up by whoever is still alive.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        handlers: Mapping[str, Handler],
        worker_id: str | None = None,
    ):
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.handlers = dict(handlers)
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self._slots = asyncio.Semaphore(settings.worker_concurrency)
        self._running: set[asyncio.Task[None]] = set()

    async def run(self, stop: asyncio.Event) -> None:
        next_reconcile = 0.0
        while not stop.is_set():
            if time.monotonic() >= next_reconcile:
                await self._reconcile()
                next_reconcile = time.monotonic() + self.settings.reconcile_seconds
            if self._slots.locked() or not await self._start_one():
                await self._idle(stop)
        # A graceful stop lets in-flight jobs finish. If the process is killed instead,
        # their leases expire and another worker retries them.
        if self._running:
            logger.info("finishing in-flight jobs", extra={"count": len(self._running)})
            await asyncio.gather(*self._running, return_exceptions=True)

    async def _idle(self, stop: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=self.settings.worker_idle_seconds)
        except TimeoutError:
            pass

    async def _reconcile(self) -> None:
        try:
            async with self.sessionmaker.begin() as session:
                expired = await reconcile_expired(session, self.settings.max_job_attempts)
        except Exception:
            logger.exception("reconcile failed")
            return
        if expired:
            logger.warning("requeued jobs whose lease expired", extra={"jobs": expired})

    async def _start_one(self) -> bool:
        if not self.handlers:
            return False
        try:
            async with self.sessionmaker.begin() as session:
                claim = await claim_next(
                    session, self.worker_id, self.settings.lease_seconds, list(self.handlers)
                )
        except Exception:
            logger.exception("claim failed")
            return False
        if claim is None:
            return False
        await self._slots.acquire()
        task = asyncio.create_task(self._work(claim))
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        return True

    async def _work(self, claim: Claim) -> None:
        async def renew() -> bool:
            async with self.sessionmaker.begin() as session:
                return await renew_lease(
                    session,
                    claim.workspace_id,
                    claim.job_id,
                    claim.lease_token,
                    self.settings.lease_seconds,
                )

        work = self.handlers[claim.kind](JobContext(claim, self.sessionmaker, self.settings))
        extra = {"job_id": claim.job_id, "kind": claim.kind}
        try:
            try:
                outcome = await hold_lease(
                    work,
                    renew,
                    lease_seconds=self.settings.lease_seconds,
                    heartbeat_seconds=self.settings.heartbeat_seconds,
                )
            except LeaseLostError:
                logger.warning("job taken away", extra=extra)
                return
            except Exception as exc:
                logger.exception("job failed", extra=extra)
                async with self.sessionmaker.begin() as session:
                    await fail(
                        session, claim.workspace_id, claim.job_id, claim.lease_token, repr(exc)
                    )
                return
            if outcome == LeaseOutcome.FINISHED:
                async with self.sessionmaker.begin() as session:
                    await finish(session, claim.workspace_id, claim.job_id, claim.lease_token)
            logger.info("job done", extra={**extra, "outcome": outcome})
        except Exception:
            logger.exception("could not record the job's end", extra=extra)
        finally:
            self._slots.release()


async def run(
    settings: Settings, stop: asyncio.Event, handlers: Mapping[str, Handler] = HANDLERS
) -> None:
    engine = create_engine(settings)
    try:
        if not await wait_for_schema(engine, stop, settings.worker_idle_seconds):
            return
        worker = Worker(create_sessionmaker(engine), settings, handlers)
        logger.info("worker ready", extra={"worker_id": worker.worker_id})
        await worker.run(stop)
    finally:
        await engine.dispose()
        logger.info("worker stopped")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run(settings, stop)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
