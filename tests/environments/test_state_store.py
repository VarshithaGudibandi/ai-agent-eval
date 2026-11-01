import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.ids import new_id
from app.environments.state_store import EnvStateStore


async def test_put_overwrites_and_list_is_ordered(
    sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt
) -> None:
    async with sessionmaker.begin() as session:
        store = EnvStateStore(session, attempt.workspace_id)
        await store.put(attempt.env_id, "things", "b", {"v": 1})
        await store.put(attempt.env_id, "things", "a", {"v": 1})
        await store.put(attempt.env_id, "things", "b", {"v": 2})
        assert await store.list_records(attempt.env_id, "things") == [{"v": 1}, {"v": 2}]
        assert await store.get(attempt.env_id, "things", "missing") is None


async def test_other_workspace_cannot_read_state(
    sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt
) -> None:
    other = models.Workspace(id=new_id("ws"), name="other")
    async with sessionmaker.begin() as session:
        session.add(other)
        await EnvStateStore(session, attempt.workspace_id).put(attempt.env_id, "c", "r", {"x": 1})
    async with sessionmaker.begin() as session:
        foreign = EnvStateStore(session, other.id)
        assert await foreign.get(attempt.env_id, "c", "r") is None
        assert await foreign.snapshot(attempt.env_id) == {}


async def test_other_workspace_cannot_write_state(
    sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt
) -> None:
    other = models.Workspace(id=new_id("ws"), name="other")
    async with sessionmaker.begin() as session:
        session.add(other)
    with pytest.raises(IntegrityError):
        async with sessionmaker.begin() as session:
            await EnvStateStore(session, other.id).put(attempt.env_id, "c", "r", {"x": 1})
