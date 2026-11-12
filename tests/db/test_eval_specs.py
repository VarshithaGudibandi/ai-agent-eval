from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.eval_specs import scorers_of
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.scoring.definitions import ScorerDefinition
from app.scoring.specs import save_definitions

RAN = ScorerDefinition(key="ran", check="run_completed")
TURNS = ScorerDefinition(key="turns", check="turn_limit", params={"max_turns": 5})


async def _agent(scope: WorkspaceScope) -> str:
    agent = models.Agent(id=new_id("agt"), workspace_id=scope.workspace_id, name=new_id("a"))
    scope.add(agent)
    await scope.flush()
    return agent.id


async def test_saving_specs_versions_only_what_changed(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        agent_id = await _agent(scope)
        first, created = await save_definitions(scope, agent_id, [RAN, TURNS])
        same, again = await save_definitions(scope, agent_id, [RAN, TURNS])
        assert (first.version, created, same.id, again) == (1, True, first.id, False)
        looser = TURNS.model_copy(update={"params": {"max_turns": 9}})
        second, _ = await save_definitions(scope, agent_id, [RAN, looser])
        reordered, _ = await save_definitions(scope, agent_id, [looser, RAN])
        v1, v2 = await scorers_of(scope, first.id), await scorers_of(scope, second.id)
        # The unchanged scorer is the same row in both versions, so its scores compare.
        assert v1[0].id == v2[0].id
        assert [(s.key, s.version) for s in v2] == [("ran", 1), ("turns", 2)]
        assert (second.version, reordered.version) == (2, 3)
