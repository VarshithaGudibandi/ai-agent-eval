from collections import Counter

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.content import load_task_file
from app.db.leakage import find_leaks
from app.environments.it_support.records import ItFixture
from app.environments.it_support.tools import TOOLS
from app.evaluators.it_spec import ItSpec
from tests.datasets.reference import run_reference, task_dir

TASKS = {path.stem: load_task_file(path) for path in sorted(task_dir("it_support").glob("*.json"))}


def test_ten_reviewed_tasks_split_by_family() -> None:
    assert len(TASKS) == 10
    assert Counter(t.split for t in TASKS.values()) == {"dev": 6, "validation": 2, "held_out": 2}
    assert all(t.review.reviewer and t.review.reference_script for t in TASKS.values())
    assert find_leaks(list(TASKS.values())) == []


@pytest.mark.parametrize("name", list(TASKS))
def test_task_world_supports_its_expected_outcome(name: str) -> None:
    task = TASKS[name]
    assert name == task.task_key.replace("-", "_")
    world = ItFixture.model_validate(task.fixture)
    spec = ItSpec.model_validate(task.evaluator_spec)
    assert task.review.reference_script is not None
    assert {s.tool for s in task.review.reference_script.steps} <= {t.name for t in TOOLS}
    requester = task.agent_input.context["authenticated_employee_id"]
    people = {e.user_id: e for e in world.employees}
    lists = {d.name: d.self_service for d in world.departments}
    assert requester in people
    # Expected effects must be ones the rules allow.
    assert all(u == requester and people[u].status == "active" for u in spec.expected_resets)
    for grant in spec.expected_grants:
        target = people[grant.user_id]
        assert target.user_id == requester or target.manager_id == requester
        assert target.status == "active"
        assert grant.system in lists[target.department]
    if spec.expected_queue is not None:
        [owned] = [t for t in world.tickets if t.ticket_id == spec.expected_queue.ticket_id]
        assert owned.requester_id == requester


@pytest.mark.parametrize("name", list(TASKS))
async def test_reference_script_passes_every_evaluator(
    name: str, sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    status, scores = await run_reference(sessionmaker, workspace_id, TASKS[name])
    failing = {
        s.dimension: (s.reason_code, s.evidence)
        for s in scores
        if s.status not in ("pass", "not_applicable")
    }
    assert status == models.EvaluationStatus.PASS, failing
    assert {s.layer for s in scores} == {"outcome", "policy", "trajectory", "response"}
    # The retail evaluators must stay out of an IT run.
    assert all(s.evaluator_name.startswith("it_") for s in scores)
