import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from app.agents.contract import RunLimits
from app.agents.scripted import Script
from app.db import models
from app.db.hashing import content_hash
from app.db.ids import new_id
from app.db.leakage import find_leaks
from app.db.scope import WorkspaceScope
from app.environments.contract import AgentTaskInput, FaultSpec


class ReviewMetadata(BaseModel):
    """Who wrote and checked a task, and what a correct run looks like.

    A task without a reviewer is a draft: it can run only in dev experiments.
    """

    model_config = ConfigDict(extra="forbid")

    author: str = Field(min_length=1)
    reviewer: str | None = None
    reviewed_on: date | None = None
    category: str = Field(pattern=r"^[a-z][a-z_]*$")
    difficulty: Literal["easy", "medium", "hard"]
    expected_outcome: str = Field(min_length=1)
    acceptable_alternatives: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    # A known-correct run the evaluators pass, so a reviewer can check the task against them.
    reference_script: Script | None = None

    @model_validator(mode="after")
    def _reviewed_together(self) -> "ReviewMetadata":
        if (self.reviewer is None) != (self.reviewed_on is None):
            raise ValueError("reviewer and reviewed_on go together")
        return self


class TaskDefinition(BaseModel):
    """One task as written in a dataset file."""

    model_config = ConfigDict(extra="forbid")

    task_key: str
    environment: str
    scenario_family: str
    split: models.Split
    agent_input: AgentTaskInput
    fixture: dict[str, Any]
    evaluator_spec: dict[str, Any]
    limits: RunLimits = RunLimits()
    faults: list[FaultSpec] = Field(default_factory=list)
    review: ReviewMetadata

    @model_validator(mode="after")
    def _one_fault_per_operation(self) -> "TaskDefinition":
        operations = [(f.tool, f.occurrence) for f in self.faults]
        if len(operations) != len(set(operations)):
            raise ValueError("a tool call can have at most one fault")
        return self


def load_task_file(path: Path) -> TaskDefinition:
    return TaskDefinition.model_validate(json.loads(path.read_text()))


class DatasetRejectedError(ValueError):
    pass


async def create_dataset_version(
    scope: WorkspaceScope, dataset_name: str, tasks: list[TaskDefinition]
) -> models.DatasetVersion:
    """Store tasks as the next immutable version, or refuse them if a split could leak."""
    leaks = find_leaks(list(tasks))
    if leaks:
        raise DatasetRejectedError(
            "; ".join(
                f"{leak.kind}: {', '.join(leak.task_keys)} ({', '.join(leak.splits)})"
                for leak in leaks
            )
        )
    workspace_id = scope.workspace_id
    dataset = await scope.first(
        scope.query(models.Dataset).where(models.Dataset.name == dataset_name)
    )
    if dataset is None:
        dataset = models.Dataset(id=new_id("ds"), workspace_id=workspace_id, name=dataset_name)
        scope.add(dataset)
        await scope.flush()
    latest = await scope.session.scalar(
        select(func.max(models.DatasetVersion.version)).where(
            models.DatasetVersion.workspace_id == workspace_id,
            models.DatasetVersion.dataset_id == dataset.id,
        )
    )
    hashes = {t.task_key: content_hash(t.model_dump(mode="json")) for t in tasks}
    version = models.DatasetVersion(
        id=new_id("dsv"),
        workspace_id=workspace_id,
        dataset_id=dataset.id,
        version=(latest or 0) + 1,
        content_hash=content_hash(hashes),
    )
    scope.add(version)
    await scope.flush()
    for task in tasks:
        scope.add(
            models.TaskVersion(
                id=new_id("task"),
                workspace_id=workspace_id,
                dataset_version_id=version.id,
                task_key=task.task_key,
                environment=task.environment,
                scenario_family=task.scenario_family,
                split=task.split,
                agent_input=task.agent_input.model_dump(),
                fixture=task.fixture,
                evaluator_spec=task.evaluator_spec,
                limits=task.limits.model_dump(),
                faults=[f.model_dump() for f in task.faults],
                category=task.review.category,
                difficulty=task.review.difficulty,
                reviewer=task.review.reviewer,
                review=task.review.model_dump(mode="json"),
                content_hash=hashes[task.task_key],
            )
        )
    await scope.flush()
    return version


async def create_agent_config(
    scope: WorkspaceScope,
    name: str,
    adapter_type: models.AdapterType,
    settings: dict[str, Any],
    limits: RunLimits,
) -> models.AgentConfig:
    body = {"adapter_type": adapter_type, "settings": settings, "limits": limits.model_dump()}
    config = models.AgentConfig(
        id=new_id("cfg"),
        workspace_id=scope.workspace_id,
        name=name,
        adapter_type=adapter_type,
        settings=settings,
        limits=limits.model_dump(),
        config_hash=content_hash(body),
    )
    scope.add(config)
    await scope.flush()
    return config


async def list_datasets(
    scope: WorkspaceScope,
) -> list[tuple[models.Dataset, list[tuple[models.DatasetVersion, int]]]]:
    datasets = await scope.all(scope.query(models.Dataset).order_by(models.Dataset.name))
    versions = await scope.all(
        scope.query(models.DatasetVersion).order_by(models.DatasetVersion.version)
    )
    rows = await scope.session.execute(
        select(models.TaskVersion.dataset_version_id, func.count())
        .where(models.TaskVersion.workspace_id == scope.workspace_id)
        .group_by(models.TaskVersion.dataset_version_id)
    )
    counts = {version_id: count for version_id, count in rows}
    return [
        (d, [(v, counts.get(v.id, 0)) for v in versions if v.dataset_id == d.id]) for d in datasets
    ]


async def list_agent_configs(scope: WorkspaceScope) -> list[models.AgentConfig]:
    return await scope.all(
        scope.query(models.AgentConfig).order_by(models.AgentConfig.created_at.desc())
    )
