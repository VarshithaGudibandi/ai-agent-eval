from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.contract import RunLimits
from app.db import models
from app.db.attempts import scores_for_attempt
from app.db.content import (
    TaskDefinition,
    create_agent_config,
    create_dataset_version,
    load_task_file,
)
from app.db.experiments import create_experiment, start_attempt
from app.db.leakage import find_leaks
from app.db.scope import WorkspaceScope
from app.environments.registry import default_registry
from app.environments.support_retail.tools import TOOLS
from app.evaluators.registry import default_evaluators
from app.evaluators.retail_spec import RetailSpec
from app.execution.attempt_runner import Platform, run_attempt

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


async def run_reference(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, task: TaskDefinition
) -> tuple[models.EvaluationStatus, list[models.Score]]:
    assert task.review.reference_script is not None
    # Unreviewed tasks run only in dev, and the split does not change how a task is graded.
    as_dev = task.model_copy(update={"split": models.Split.DEV})
    platform = Platform(sessionmaker, default_registry(), default_evaluators())
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        version = await create_dataset_version(scope, task.task_key, [as_dev])
        config = await create_agent_config(
            scope,
            "reference",
            models.AdapterType.SCRIPTED,
            {"script": task.review.reference_script.model_dump()},
            RunLimits(),
        )
        experiment = await create_experiment(
            scope, version.id, [config.id], platform.evaluators, mode=models.ExperimentMode.DEMO
        )
        [run] = await scope.all(
            scope.query(models.Run).where(models.Run.experiment_id == experiment.id)
        )
        attempt = await start_attempt(scope, run.id)
    status = await run_attempt(platform, workspace_id, attempt.id)
    async with sessionmaker() as session:
        scores = await scores_for_attempt(WorkspaceScope(session, workspace_id), attempt.id)
    return status, scores
