import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.datasets import CaseData, save_version
from app.db.experiments import start_experiment
from app.db.scope import WorkspaceScope
from app.db.traces import agent_version


async def test_a_retry_that_overlaps_the_first_start_gets_the_same_experiment(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        dataset, _ = await save_version(scope, "d", [CaseData("c1", "hi")])
        version = await agent_version(scope, "bot", "f", {}, None)
    first_session, retry_session = sessionmaker(), sessionmaker()
    async with first_session, retry_session:
        first_scope = WorkspaceScope(first_session, workspace_id)
        retry_scope = WorkspaceScope(retry_session, workspace_id)
        first, created = await start_experiment(first_scope, version, dataset.id, 1, "k")
        # The retry misses the uncommitted first row and waits on the key's unique index.
        retry = asyncio.create_task(start_experiment(retry_scope, version, dataset.id, 1, "k"))
        await asyncio.sleep(0.3)
        assert not retry.done()
        await first_session.commit()
        replayed, replay_created = await retry
        await retry_session.commit()
    assert (created, replay_created) == (True, False)
    assert replayed.id == first.id
