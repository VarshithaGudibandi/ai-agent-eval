import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.agents.contract import RunLimits
from app.db import models
from app.db.hashing import content_hash
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.environments.contract import AgentTaskInput


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


def load_task_file(path: Path) -> TaskDefinition:
    return TaskDefinition.model_validate(json.loads(path.read_text()))


async def create_dataset_version(
    scope: WorkspaceScope, dataset_name: str, tasks: list[TaskDefinition]
) -> models.DatasetVersion:
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
