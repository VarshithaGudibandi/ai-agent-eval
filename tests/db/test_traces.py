import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.db.traces import agent_version, store_raw

Sessions = async_sessionmaker[AsyncSession]


async def test_a_trace_id_is_stored_once_per_workspace(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    other = models.Workspace(id=new_id("ws"), name="other")
    async with sessionmaker.begin() as session:
        session.add(other)
    async with sessionmaker.begin() as session:
        mine = WorkspaceScope(session, workspace_id)
        first = await store_raw(mine, "t-1", "bot", {"n": 1})
        again = await store_raw(mine, "t-1", "bot", {"n": 2})
        theirs = await store_raw(WorkspaceScope(session, other.id), "t-1", "bot", {"n": 3})
    assert first is not None and again is None and theirs is not None
    assert (first.status, first.payload) == ("received", {"n": 1})


async def test_concurrent_first_traces_of_a_version_make_one_agent_and_one_version(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    async def make() -> str:
        async with sessionmaker.begin() as session:
            version = await agent_version(
                WorkspaceScope(session, workspace_id), "bot", "f" * 64, {"model": "m"}, None
            )
            return version.id

    ids = await asyncio.gather(*(make() for _ in range(5)))
    assert len(set(ids)) == 1
    async with sessionmaker() as session:
        agents = await session.scalar(
            select(func.count())
            .select_from(models.Agent)
            .where(models.Agent.workspace_id == workspace_id)
        )
    assert agents == 1


async def test_a_label_names_an_unlabelled_version_but_never_renames_one(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        unnamed = await agent_version(scope, "bot", "a" * 64, {}, None)
        named = await agent_version(scope, "bot", "a" * 64, {}, "v1")
        renamed = await agent_version(scope, "bot", "a" * 64, {}, "v9")
    assert unnamed.id == named.id == renamed.id
    assert renamed.label == "v1"
