import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.contract import RunLimits
from app.db import models
from app.db.content import create_agent_config, create_dataset_version, load_task_file

TASK_FILE = (
    Path(__file__).parents[2] / "datasets/support_retail/tasks/refund_damaged_headphones.json"
)


def test_task_file_loads() -> None:
    task = load_task_file(TASK_FILE)
    assert task.environment == "support-retail@1"
    assert task.limits.max_tool_calls == 10


def test_unknown_task_fields_are_rejected(tmp_path: Path) -> None:
    body = json.loads(TASK_FILE.read_text())
    body["expected_answer"] = "leak"
    path = tmp_path / "task.json"
    path.write_text(json.dumps(body))
    with pytest.raises(ValidationError):
        load_task_file(path)


async def test_versions_count_up_and_hash_follows_content(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    task = load_task_file(TASK_FILE)
    changed = task.model_copy(update={"scenario_family": "other"})
    async with sessionmaker.begin() as session:
        first = await create_dataset_version(session, workspace_id, "d", [task])
        same = await create_dataset_version(session, workspace_id, "d", [task])
        different = await create_dataset_version(session, workspace_id, "d", [changed])
        rows = list(
            await session.scalars(
                select(models.TaskVersion).where(models.TaskVersion.dataset_version_id == first.id)
            )
        )
    assert [first.version, same.version, different.version] == [1, 2, 3]
    assert first.content_hash == same.content_hash != different.content_hash
    assert [r.task_key for r in rows] == ["refund-damaged-headphones"]


async def test_config_hash_ignores_name(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        a = await create_agent_config(
            session, workspace_id, "a", models.AdapterType.SCRIPTED, {"x": 1}, RunLimits()
        )
        b = await create_agent_config(
            session, workspace_id, "b", models.AdapterType.SCRIPTED, {"x": 1}, RunLimits()
        )
        c = await create_agent_config(
            session, workspace_id, "c", models.AdapterType.SCRIPTED, {"x": 2}, RunLimits()
        )
    assert a.config_hash == b.config_hash != c.config_hash
