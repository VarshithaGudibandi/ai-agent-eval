from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.content import load_task_file
from app.db.leakage import find_leaks
from app.environments.support_retail.tools import TOOLS
from app.evaluators.retail_spec import RetailSpec
from tests.datasets.support_retail.reference import run_reference

TASK_DIR = Path(__file__).parents[3] / "datasets/support_retail/tasks"
FILES = sorted(TASK_DIR.glob("*.json"))
TASKS = {path.stem: load_task_file(path) for path in FILES}

CATEGORY_COUNTS = {
    "lookup": 8,
    "eligible_refund": 9,
    "ineligible_refund": 8,
    "missing_information": 6,
    "access_boundary": 5,
    "tool_failure": 6,
    "duplicate_operation": 4,
    "untrusted_content": 4,
}


def test_the_dataset_has_fifty_tasks_in_the_planned_categories() -> None:
    assert Counter(t.review.category for t in TASKS.values()) == CATEGORY_COUNTS


def test_splits_are_sixty_twenty_twenty_and_every_category_is_in_each() -> None:
    assert Counter(t.split for t in TASKS.values()) == {"dev": 30, "validation": 10, "held_out": 10}
    for split in models.Split:
        assert {t.review.category for t in TASKS.values() if t.split == split} == set(
            CATEGORY_COUNTS
        )


def test_nothing_leaks_between_splits() -> None:
    assert find_leaks(list(TASKS.values())) == []


@pytest.mark.parametrize("name", list(TASKS))
def test_task_file_is_consistent(name: str) -> None:
    task = TASKS[name]
    assert name == task.task_key.replace("-", "_")
    RetailSpec.model_validate(task.evaluator_spec)
    tools = {t.name for t in TOOLS}
    assert {f.tool for f in task.faults} <= tools
    assert task.review.reference_script is not None
    assert {s.tool for s in task.review.reference_script.steps} <= tools
    # The agent's input never carries what only the grader should see.
    visible = task.agent_input.model_dump_json()
    assert "reviewer" not in visible and "expected" not in visible


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
    assert {s.status for s in scores} <= {"pass", "not_applicable"}
