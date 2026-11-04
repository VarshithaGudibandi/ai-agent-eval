import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.ids import new_id
from app.db.reports import run_facts
from app.db.scope import NotFoundError, WorkspaceScope
from tests.conftest import GOOD_SCRIPT, UNAPPROVED_SCRIPT, RunDemo


async def test_facts_cover_every_planned_run_with_its_graded_attempt(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, run_demo: RunDemo
) -> None:
    demo = await run_demo(sessionmaker, workspace_id, [GOOD_SCRIPT, UNAPPROVED_SCRIPT], 2)
    good, unapproved = demo.config_ids
    async with sessionmaker() as session:
        facts = await run_facts(WorkspaceScope(session, workspace_id), demo.experiment_id)
    assert [(f.agent_config_id, f.repetition) for f in facts] == sorted(
        [(good, 1), (good, 2), (unapproved, 1), (unapproved, 2)]
    )
    for fact in facts:
        assert fact.attempts == 1
        assert fact.attempt_id is not None
        assert fact.queue_ms is not None and fact.duration_ms is not None
        assert fact.task_key == "refund-damaged-headphones"
        assert {s.layer for s in fact.scores} == {"outcome", "policy", "trajectory", "response"}
    by_config = {f.agent_config_id: f for f in facts}
    assert by_config[good].evaluation_status == "pass"
    assert (by_config[good].tool_calls_attempted, by_config[good].tool_calls_invalid) == (4, 0)
    assert by_config[unapproved].evaluation_status == "fail"
    assert (
        by_config[unapproved].tool_calls_attempted,
        by_config[unapproved].tool_calls_invalid,
    ) == (
        3,
        1,
    )
    # Scripted agents make no model calls, so nothing was metered.
    assert by_config[good].model_requests == 0 and by_config[good].cost_known


async def test_another_workspace_cannot_read_the_facts(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, run_demo: RunDemo
) -> None:
    demo = await run_demo(sessionmaker, workspace_id, [GOOD_SCRIPT])
    other = models.Workspace(id=new_id("ws"), name="other")
    async with sessionmaker.begin() as session:
        session.add(other)
    async with sessionmaker() as session:
        with pytest.raises(NotFoundError):
            await run_facts(WorkspaceScope(session, other.id), demo.experiment_id)
