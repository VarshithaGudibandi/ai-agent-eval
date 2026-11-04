import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.comparisons import SideUnclearError, list_comparisons, load_side, save_comparison
from app.db.scope import WorkspaceScope
from tests.conftest import GOOD_SCRIPT, UNAPPROVED_SCRIPT, RunDemo


async def test_a_single_config_experiment_needs_no_config_named(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, run_demo: RunDemo
) -> None:
    demo = await run_demo(sessionmaker, workspace_id, [GOOD_SCRIPT], 2)
    async with sessionmaker() as session:
        side, runs = await load_side(
            WorkspaceScope(session, workspace_id), demo.experiment_id, None
        )
    assert side.agent_config_id == demo.config_ids[0]
    assert side.status == "completed"
    assert side.environments == ["support-retail@1"]
    assert [r.repetition for r in runs] == [1, 2]


async def test_a_side_keeps_only_its_own_configs_runs(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, run_demo: RunDemo
) -> None:
    demo = await run_demo(sessionmaker, workspace_id, [GOOD_SCRIPT, UNAPPROVED_SCRIPT])
    async with sessionmaker() as session:
        scope = WorkspaceScope(session, workspace_id)
        _, runs = await load_side(scope, demo.experiment_id, demo.config_ids[1])
        assert {r.agent_config_id for r in runs} == {demo.config_ids[1]}
        with pytest.raises(SideUnclearError):
            await load_side(scope, demo.experiment_id, None)
        with pytest.raises(SideUnclearError):
            await load_side(scope, demo.experiment_id, "cfg_elsewhere")


async def test_saved_comparisons_list_newest_first(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, run_demo: RunDemo
) -> None:
    demo = await run_demo(sessionmaker, workspace_id, [GOOD_SCRIPT, UNAPPROVED_SCRIPT])
    ids = []
    for _ in range(2):
        # Separate transactions, so the rows get different creation times.
        async with sessionmaker.begin() as session:
            scope = WorkspaceScope(session, workspace_id)
            base, _ = await load_side(scope, demo.experiment_id, demo.config_ids[0])
            cand, _ = await load_side(scope, demo.experiment_id, demo.config_ids[1])
            ids.append(save_comparison(scope, base, cand, {"wins": 0}).id)
    async with sessionmaker() as session:
        listed = await list_comparisons(WorkspaceScope(session, workspace_id))
    assert [c.id for c in listed] == ids[::-1]
