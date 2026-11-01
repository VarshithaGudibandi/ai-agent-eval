import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.execution.trace_recorder import AttemptNotFoundError, TraceRecorder


async def test_events_get_consecutive_sequence_numbers(
    sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt
) -> None:
    recorder = TraceRecorder(sessionmaker, attempt.workspace_id, attempt.id)
    ids = [await recorder.record("note", {"n": n}) for n in range(3)]
    async with sessionmaker() as session:
        rows = (
            await session.scalars(
                select(models.TraceEvent)
                .where(models.TraceEvent.attempt_id == attempt.id)
                .order_by(models.TraceEvent.sequence_number)
            )
        ).all()
    assert [r.id for r in rows] == ids
    assert [r.sequence_number for r in rows] == [1, 2, 3]
    assert [r.payload for r in rows] == [{"n": 0}, {"n": 1}, {"n": 2}]


async def test_rolled_back_event_does_not_use_a_sequence_number(
    sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt
) -> None:
    recorder = TraceRecorder(sessionmaker, attempt.workspace_id, attempt.id)
    with pytest.raises(RuntimeError):
        async with sessionmaker.begin() as session:
            await recorder.record_in(session, "note", {})
            raise RuntimeError
    await recorder.record("note", {})
    async with sessionmaker() as session:
        sequences = (
            await session.scalars(
                select(models.TraceEvent.sequence_number).where(
                    models.TraceEvent.attempt_id == attempt.id
                )
            )
        ).all()
    assert sequences == [1]


async def test_recording_into_another_workspace_fails(
    sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt
) -> None:
    recorder = TraceRecorder(sessionmaker, "ws_other", attempt.id)
    with pytest.raises(AttemptNotFoundError):
        await recorder.record("note", {})
