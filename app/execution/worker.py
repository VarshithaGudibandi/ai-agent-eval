import asyncio
import logging
import signal
import socket
import time
import uuid

from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.attempts import AttemptNotLiveError
from app.db.engine import create_engine, create_sessionmaker
from app.db.jobs import Claim, claim_next, reconcile_expired, renew_lease
from app.db.schema_status import schema_status
from app.environments.registry import default_registry
from app.evaluators.registry import default_evaluators
from app.execution.attempt_runner import Platform, run_attempt
from app.execution.leases import hold_lease
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


class Worker:
    """Claims jobs from the Postgres queue and runs their attempts, a few at a time.

    Workers are stateless and share nothing but the database, so more of them simply means
    more throughput. Each also reconciles expired leases, so a crashed worker's runs are
    picked up by whoever is still alive.
    """

    def __init__(self, platform: Platform, settings: Settings, worker_id: str | None = None):
        self.platform = platform
        self.settings = settings
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
        # A graceful stop lets in-flight attempts finish. If the process is killed instead,
        # their leases expire and another worker retries them.
        if self._running:
            logger.info("finishing in-flight attempts", extra={"count": len(self._running)})
            await asyncio.gather(*self._running, return_exceptions=True)

    async def _idle(self, stop: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=self.settings.worker_idle_seconds)
        except TimeoutError:
            pass

    async def _reconcile(self) -> None:
        try:
            async with self.platform.sessionmaker.begin() as session:
                interrupted = await reconcile_expired(session, self.settings.max_attempts_per_run)
        except Exception:
            logger.exception("reconcile failed")
            return
        if interrupted:
            logger.warning("interrupted expired attempts", extra={"attempts": interrupted})

    async def _start_one(self) -> bool:
        try:
            async with self.platform.sessionmaker.begin() as session:
                claim = await claim_next(session, self.worker_id, self.settings.lease_seconds)
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
            async with self.platform.sessionmaker.begin() as session:
                return await renew_lease(
                    session,
                    claim.workspace_id,
                    claim.attempt_id,
                    claim.lease_token,
                    self.settings.lease_seconds,
                )

        try:
            outcome = await hold_lease(
                run_attempt(self.platform, claim.workspace_id, claim.attempt_id, claim.lease_token),
                renew,
                lease_seconds=self.settings.lease_seconds,
                heartbeat_seconds=self.settings.heartbeat_seconds,
            )
            logger.info("attempt done", extra={"attempt_id": claim.attempt_id, "outcome": outcome})
        except AttemptNotLiveError:
            logger.warning("attempt taken away", extra={"attempt_id": claim.attempt_id})
        except Exception:
            logger.exception("attempt crashed", extra={"attempt_id": claim.attempt_id})
        finally:
            self._slots.release()


async def run(settings: Settings, stop: asyncio.Event) -> None:
    engine = create_engine(settings)
    try:
        if not await wait_for_schema(engine, stop, settings.worker_idle_seconds):
            return
        platform = Platform(
            create_sessionmaker(engine),
            default_registry(),
            default_evaluators(),
            settings=settings,
        )
        worker = Worker(platform, settings)
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
