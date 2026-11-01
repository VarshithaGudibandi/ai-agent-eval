import copy
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.contract import RunLimits
from app.db import models
from app.db.content import (
    TaskDefinition,
    create_agent_config,
    create_dataset_version,
    load_task_file,
)
from app.db.experiments import ExperimentRejectedError, create_experiment, start_attempt
from app.db.ids import new_id
from app.db.scope import NotFoundError, WorkspaceScope
from app.evaluators.registry import default_evaluators

TASK_FILE = (
    Path(__file__).parents[2] / "datasets/support_retail/tasks/refund_damaged_headphones.json"
)


async def _setup(session: AsyncSession, workspace_id: str) -> tuple[str, list[str]]:
    task = load_task_file(TASK_FILE)
    second = task.model_copy(update={"task_key": "second"})
    version = await create_dataset_version(
        WorkspaceScope(session, workspace_id), "d", [task, second]
    )
    configs = [
        await create_agent_config(
            WorkspaceScope(session, workspace_id),
            name,
            models.AdapterType.SCRIPTED,
            {"n": name},
            RunLimits(),
        )
        for name in ("a", "b")
    ]
    return version.id, [c.id for c in configs]


async def test_runs_cover_every_config_task_and_repetition(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        version_id, config_ids = await _setup(session, workspace_id)
        experiment = await create_experiment(
            WorkspaceScope(session, workspace_id),
            version_id,
            config_ids,
            default_evaluators(),
            mode=models.ExperimentMode.DEMO,
            repetitions=2,
        )
        runs = list(
            await session.scalars(
                select(models.Run).where(models.Run.experiment_id == experiment.id)
            )
        )
    combos = {(r.agent_config_id, r.task_version_id, r.repetition) for r in runs}
    assert len(runs) == len(combos) == 2 * 2 * 2
    assert {r.agent_config_id for r in runs} == set(config_ids)
    assert experiment.manifest["evaluators"] == sorted(
        ({"name": e.name, "version": e.version} for e in default_evaluators()),
        key=lambda e: e["name"],
    )
    assert [c["id"] for c in experiment.manifest["agent_configs"]] == config_ids


async def test_same_idempotency_key_returns_the_same_experiment(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        version_id, config_ids = await _setup(session, workspace_id)
        first = await create_experiment(
            WorkspaceScope(session, workspace_id),
            version_id,
            config_ids,
            [],
            mode=models.ExperimentMode.DEMO,
            idempotency_key="k",
        )
        again = await create_experiment(
            WorkspaceScope(session, workspace_id),
            version_id,
            config_ids,
            [],
            mode=models.ExperimentMode.DEMO,
            idempotency_key="k",
        )
    assert first.id == again.id


async def test_unknown_config_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        version_id, _ = await _setup(session, workspace_id)
        with pytest.raises(LookupError):
            await create_experiment(
                WorkspaceScope(session, workspace_id),
                version_id,
                ["cfg_missing"],
                [],
                mode=models.ExperimentMode.DEMO,
            )


async def test_each_attempt_gets_the_next_number_and_a_fresh_environment(
    sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt
) -> None:
    async with sessionmaker.begin() as session:
        second = await start_attempt(WorkspaceScope(session, attempt.workspace_id), attempt.run_id)
    assert second.attempt_number == 2
    assert second.env_id != attempt.env_id


async def test_scripted_agents_cannot_run_in_a_real_experiment(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        version_id, config_ids = await _setup(session, workspace_id)
        with pytest.raises(ExperimentRejectedError):
            await create_experiment(
                scope, version_id, config_ids, [], mode=models.ExperimentMode.REAL
            )


async def test_ids_from_another_workspace_are_not_found(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    other = models.Workspace(id=new_id("ws"), name="other")
    async with sessionmaker.begin() as session:
        session.add(other)
        version_id, _ = await _setup(session, workspace_id)
        with pytest.raises(NotFoundError):
            await create_experiment(
                WorkspaceScope(session, other.id),
                version_id,
                [],
                [],
                mode=models.ExperimentMode.DEMO,
            )


def _variant(split: str, total: int, message: str, reviewed: bool) -> TaskDefinition:
    task = load_task_file(TASK_FILE)
    fixture = copy.deepcopy(task.fixture)
    fixture["orders"][0]["total_minor"] = total
    review = task.review.model_copy(
        update={"reviewer": "R. Viewer", "reviewed_on": date(2026, 11, 1)} if reviewed else {}
    )
    return task.model_copy(
        update={
            "task_key": f"{split}-task",
            "scenario_family": f"{split}-family",
            "split": models.Split(split),
            "agent_input": task.agent_input.model_copy(update={"user_message": message}),
            "fixture": fixture,
            "review": review,
        }
    )


async def _split_dataset(
    session: AsyncSession, workspace_id: str, reviewed: bool
) -> tuple[str, str]:
    scope = WorkspaceScope(session, workspace_id)
    version = await create_dataset_version(
        scope,
        "splits",
        [
            _variant("dev", 24999, "Refund my headphones, they broke.", reviewed=False),
            _variant("validation", 31000, "Where is the money for my returned speaker?", reviewed),
            _variant(
                "held_out", 45000, "The monitor I sent back was fine, why no refund?", reviewed
            ),
        ],
    )
    config = await create_agent_config(scope, "c", models.AdapterType.SCRIPTED, {}, RunLimits())
    return version.id, config.id


async def _create(
    session: AsyncSession, workspace_id: str, version_id: str, config_id: str, **kwargs: Any
) -> models.Experiment:
    return await create_experiment(
        WorkspaceScope(session, workspace_id),
        version_id,
        [config_id],
        [],
        mode=models.ExperimentMode.DEMO,
        **kwargs,
    )


async def test_an_experiment_runs_one_split_and_records_it(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        version_id, config_id = await _split_dataset(session, workspace_id, reviewed=True)
        dev = await _create(session, workspace_id, version_id, config_id)
        validation = await _create(
            session, workspace_id, version_id, config_id, split=models.Split.VALIDATION
        )
        tasks = {
            e.id: {
                t.task_key
                for t in await session.scalars(
                    select(models.TaskVersion)
                    .join(models.Run, models.Run.task_version_id == models.TaskVersion.id)
                    .where(models.Run.experiment_id == e.id)
                )
            }
            for e in (dev, validation)
        }
    assert tasks == {dev.id: {"dev-task"}, validation.id: {"validation-task"}}
    assert (validation.split, validation.manifest["split"]) == ("validation", "validation")


async def test_held_out_tasks_need_a_final_experiment(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        version_id, config_id = await _split_dataset(session, workspace_id, reviewed=True)
        with pytest.raises(ExperimentRejectedError, match="final"):
            await _create(session, workspace_id, version_id, config_id, split=models.Split.HELD_OUT)
        final = await _create(
            session, workspace_id, version_id, config_id, split=models.Split.HELD_OUT, final=True
        )
    assert final.final


async def test_unreviewed_tasks_run_only_in_dev(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        version_id, config_id = await _split_dataset(session, workspace_id, reviewed=False)
        # The dev task is unreviewed too, and dev is where drafts are allowed.
        await _create(session, workspace_id, version_id, config_id)
        with pytest.raises(ExperimentRejectedError, match="unreviewed tasks run only in dev"):
            await _create(
                session, workspace_id, version_id, config_id, split=models.Split.VALIDATION
            )


async def test_a_split_without_tasks_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        version_id, config_ids = await _setup(session, workspace_id)
        with pytest.raises(ExperimentRejectedError, match="no validation tasks"):
            await _create(
                session, workspace_id, version_id, config_ids[0], split=models.Split.VALIDATION
            )
