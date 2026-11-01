import asyncio
import logging
import signal

from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.engine import create_engine
from app.db.schema_status import schema_status
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


async def run(settings: Settings, stop: asyncio.Event) -> None:
    engine = create_engine(settings)
    try:
        if not await wait_for_schema(engine, stop, settings.worker_idle_seconds):
            return
        logger.info("worker ready")
        await stop.wait()
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
