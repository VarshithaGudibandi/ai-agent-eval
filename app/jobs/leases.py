import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class LeaseOutcome(StrEnum):
    FINISHED = "finished"
    # The lease was refused on renewal: expired, taken over, or revoked by a cancel.
    LOST = "lost"
    # Renewals kept failing, so the worker stopped before its lease could run out.
    SELF_STOPPED = "self_stopped"


async def hold_lease(
    work: Coroutine[Any, Any, Any],
    renew: Callable[[], Awaitable[bool]],
    *,
    lease_seconds: float,
    heartbeat_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> LeaseOutcome:
    """Run work while renewing its lease, and stop it the moment the lease is in doubt.

    The worker stops itself while at least one heartbeat interval of lease is left, so it
    has given up before the reconciler can hand the attempt to anyone else.
    """
    task = asyncio.create_task(work)
    valid_until = clock() + lease_seconds
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=heartbeat_seconds)
            if done:
                task.result()
                return LeaseOutcome.FINISHED
            asked_at = clock()
            try:
                if not await renew():
                    return await _stop(task, LeaseOutcome.LOST)
                valid_until = asked_at + lease_seconds
            except Exception:
                logger.warning("lease renewal failed", exc_info=True)
            if clock() >= valid_until - heartbeat_seconds:
                return await _stop(task, LeaseOutcome.SELF_STOPPED)
    finally:
        if not task.done():
            task.cancel()


async def _stop(task: asyncio.Task[Any], outcome: LeaseOutcome) -> LeaseOutcome:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning("work raised while stopping", exc_info=True)
    logger.warning("stopped work after losing the lease", extra={"outcome": outcome})
    return outcome
