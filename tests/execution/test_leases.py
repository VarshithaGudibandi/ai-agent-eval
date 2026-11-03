import asyncio
import time

import pytest

from app.execution.leases import LeaseOutcome, hold_lease


class Work:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.cancelled = False

    async def __call__(self) -> None:
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_work_that_finishes_keeps_its_lease() -> None:
    renewals = 0

    async def renew() -> bool:
        nonlocal renewals
        renewals += 1
        return True

    work = Work(0.25)
    outcome = await hold_lease(work(), renew, lease_seconds=0.4, heartbeat_seconds=0.1)
    assert outcome == LeaseOutcome.FINISHED
    assert renewals >= 1 and not work.cancelled


async def test_a_lost_lease_stops_the_work_at_once() -> None:
    async def renew() -> bool:
        return False

    work = Work(10)
    started = time.monotonic()
    outcome = await hold_lease(work(), renew, lease_seconds=1, heartbeat_seconds=0.1)
    assert outcome == LeaseOutcome.LOST
    assert work.cancelled and time.monotonic() - started < 0.5


async def test_a_worker_that_cannot_renew_stops_before_its_lease_ends() -> None:
    async def renew() -> bool:
        raise ConnectionError("database unreachable")

    work = Work(10)
    started = time.monotonic()
    outcome = await hold_lease(work(), renew, lease_seconds=0.6, heartbeat_seconds=0.15)
    elapsed = time.monotonic() - started
    assert outcome == LeaseOutcome.SELF_STOPPED
    assert work.cancelled
    # Stopped with time to spare: nobody else could have been handed the attempt yet.
    assert elapsed < 0.6


async def test_errors_from_the_work_are_raised() -> None:
    async def failing() -> None:
        raise ValueError("boom")

    async def renew() -> bool:
        return True

    with pytest.raises(ValueError, match="boom"):
        await hold_lease(failing(), renew, lease_seconds=1, heartbeat_seconds=0.2)
