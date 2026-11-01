import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.rate_limits import take
from app.db.scope import WorkspaceScope
from app.providers.contract import Retryable

Sleep = Callable[[float], Awaitable[None]]


class RateLimiter:
    """Keeps every worker together under one key's requests and tokens per minute."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        workspace_id: str,
        credential_id: str,
        *,
        max_wait_seconds: float = 300,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._workspace_id = workspace_id
        self._credential_id = credential_id
        self._max_wait = max_wait_seconds
        self._sleep = sleep

    async def acquire(self, tokens: int) -> float:
        """Wait until the call may go. Returns how long it waited."""
        waited = 0.0
        while True:
            async with self._sessionmaker.begin() as session:
                wait = await take(
                    WorkspaceScope(session, self._workspace_id), self._credential_id, tokens
                )
            if wait == 0:
                return waited
            if waited + wait > self._max_wait:
                raise Retryable(f"rate limiter would wait more than {self._max_wait:.0f}s")
            await self._sleep(wait)
            waited += wait
