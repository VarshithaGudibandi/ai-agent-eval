import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.providers.contract import Retryable
from app.providers.rate_limiter import RateLimiter
from tests.db.test_rate_limits import credential_with_limits


async def test_waits_then_gives_up_past_the_maximum(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, credential_id = await credential_with_limits(sessionmaker, rpm=1, tpm=None)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    limiter = RateLimiter(
        sessionmaker, workspace_id, credential_id, max_wait_seconds=100, sleep=fake_sleep
    )
    assert await limiter.acquire(1) == 0
    with pytest.raises(Retryable):
        await limiter.acquire(1)
    assert slept and all(s > 50 for s in slept)
