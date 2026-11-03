import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.attempts import AttemptNotLiveError, lease_holds
from app.db.ids import new_id


class AttemptNotFoundError(LookupError):
    pass


class TraceRecorder:
    """Writes the ordered trace of one attempt.

    Sequence numbers come from a counter on the attempt row, so they stay gap-free and
    ordered even when several processes record events for the same attempt. With a lease
    token, bumping that counter is also the fence: it only succeeds while the lease holds,
    and tool and model calls write their events in the same transaction as their effects.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        workspace_id: str,
        attempt_id: str,
        lease_token: str | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._workspace_id = workspace_id
        self._attempt_id = attempt_id
        self._lease_token = lease_token
        self._started = time.monotonic()

    async def record(self, event_type: str, payload: dict[str, Any]) -> str:
        async with self._sessionmaker.begin() as session:
            return await self.record_in(session, event_type, payload)

    async def record_in(
        self, session: AsyncSession, event_type: str, payload: dict[str, Any]
    ) -> str:
        """Record inside the caller's transaction, so the event commits with its effects."""
        condition = (
            lease_holds(self._attempt_id, self._lease_token)
            if self._lease_token is not None
            else models.Attempt.id == self._attempt_id
        )
        sequence = await session.scalar(
            update(models.Attempt)
            .where(models.Attempt.workspace_id == self._workspace_id, condition)
            .values(last_event_sequence=models.Attempt.last_event_sequence + 1)
            .returning(models.Attempt.last_event_sequence)
        )
        if sequence is None:
            if self._lease_token is not None:
                raise AttemptNotLiveError(self._attempt_id)
            raise AttemptNotFoundError(self._attempt_id)
        event = models.TraceEvent(
            id=new_id("evt"),
            workspace_id=self._workspace_id,
            attempt_id=self._attempt_id,
            sequence_number=sequence,
            event_type=event_type,
            payload=payload,
            occurred_at=datetime.now(UTC),
            elapsed_ms=round((time.monotonic() - self._started) * 1000),
        )
        session.add(event)
        await session.flush()
        return event.id
