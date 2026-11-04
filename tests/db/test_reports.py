from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.contract import RunLimits
from app.db import models
from app.db.content import create_agent_config, create_dataset_version, load_task_file
from app.db.experiments import create_experiment
from app.db.ids import new_id
from app.db.reports import held_out_runs, run_facts
from app.db.scope import NotFoundError, WorkspaceScope
from tests.conftest import GOOD_SCRIPT, UNAPPROVED_SCRIPT, RunDemo

HELD_OUT_TASK = (
    Path(__file__).parents[2] / "datasets/support_retail/tasks/boundary_brothers_order.json"
)


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


async def test_held_out_runs_are_counted_per_config(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    held_out = load_task_file(HELD_OUT_TASK)
    assert held_out.split == "held_out"
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        version = await create_dataset_version(scope, "d", [held_out])
        seen, unseen = [
            await create_agent_config(
                scope, name, models.AdapterType.SCRIPTED, {"script": GOOD_SCRIPT}, RunLimits()
            )
            for name in ("seen", "unseen")
        ]
        await create_experiment(
            scope,
            version.id,
            [seen.id],
            [],
            mode=models.ExperimentMode.DEMO,
            split=models.Split.HELD_OUT,
            final=True,
            repetitions=3,
        )
        assert await held_out_runs(scope, [seen.id, unseen.id]) == {seen.id: 3, unseen.id: 0}
        assert await held_out_runs(scope, []) == {}
