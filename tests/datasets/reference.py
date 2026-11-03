from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.contract import RunLimits
from app.db import models
from app.db.attempts import scores_for_attempt, trace_events
from app.db.content import (
    TaskDefinition,
    create_agent_config,
    create_dataset_version,
    load_task_file,
)
from app.db.experiments import create_experiment, start_attempt
from app.db.scope import WorkspaceScope
from app.environments.contract import TaskVersion
from app.environments.registry import default_registry
from app.evaluators.contract import EvalContext, TraceEvent
from app.evaluators.registry import default_evaluators
from app.execution.attempt_runner import Platform, run_attempt


async def run_reference_attempt(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, task: TaskDefinition
) -> tuple[models.EvaluationStatus, str]:
    """Run a task's reference script through the real store, gateway and evaluators."""
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
    return await run_attempt(platform, workspace_id, attempt.id), attempt.id


async def run_reference(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, task: TaskDefinition
) -> tuple[models.EvaluationStatus, list[models.Score]]:
    status, attempt_id = await run_reference_attempt(sessionmaker, workspace_id, task)
    async with sessionmaker() as session:
        scores = await scores_for_attempt(WorkspaceScope(session, workspace_id), attempt_id)
    return status, scores


async def golden_context(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, task: TaskDefinition
) -> EvalContext:
    """What the evaluators saw on a reference run, as recorded in the database."""
    status, attempt_id = await run_reference_attempt(sessionmaker, workspace_id, task)
    assert status == models.EvaluationStatus.PASS
    async with sessionmaker() as session:
        scope = WorkspaceScope(session, workspace_id)
        attempt = await scope.require(models.Attempt, attempt_id)
        events = await trace_events(scope, attempt_id)
        [final] = await scope.all(
            scope.query(models.EnvSnapshot).where(
                models.EnvSnapshot.attempt_id == attempt_id,
                models.EnvSnapshot.kind == models.SnapshotKind.FINAL,
            )
        )
    return EvalContext(
        task=TaskVersion(
            id="golden",
            task_key=task.task_key,
            environment=task.environment,
            scenario_family=task.scenario_family,
            split=task.split,
            agent_input=task.agent_input,
            fixture=task.fixture,
            evaluator_spec=task.evaluator_spec,
            faults=tuple(task.faults),
        ),
        final_snapshot=final.state,
        trace=[
            TraceEvent(
                id=e.id,
                sequence_number=e.sequence_number,
                event_type=e.event_type,
                payload=e.payload,
            )
            for e in events
        ],
        final_response=attempt.final_response or "",
    )


DATASETS = Path(__file__).parents[2] / "datasets"


def task_dir(dataset: str) -> Path:
    return DATASETS / dataset / "tasks"


def load_task(dataset: str, name: str) -> TaskDefinition:
    return load_task_file(task_dir(dataset) / f"{name}.json")
