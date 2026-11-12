import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.datasets import CaseData, save_version
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.db.traces import agent_version


async def _versions(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> tuple[str, str]:
    """A dataset version and an agent version to hang an experiment on."""
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        dataset, _ = await save_version(scope, new_id("d"), [CaseData("c1", "hi")])
        agent = await agent_version(scope, "bot", new_id("f"), {}, None)
    return dataset.id, agent.id


def _experiment(workspace_id: str, versions: tuple[str, str], **overrides: object) -> models.Base:
    values: dict[str, object] = {
        "id": new_id("exp"),
        "workspace_id": workspace_id,
        "dataset_version_id": versions[0],
        "agent_version_id": versions[1],
        "status": "running",
        "repetitions": 1,
    }
    values.update(overrides)
    return models.Experiment(**values)


def _job(workspace_id: str, **overrides: object) -> models.Base:
    values: dict[str, object] = {
        "id": new_id("job"),
        "workspace_id": workspace_id,
        "kind": "model_test",
        "subject_id": "trace_1",
        "status": "queued",
    }
    values.update(overrides)
    return models.Job(**values)


async def _rejected(sessionmaker: async_sessionmaker[AsyncSession], *rows: models.Base) -> None:
    with pytest.raises(IntegrityError):
        async with sessionmaker.begin() as session:
            for row in rows:
                session.add(row)
                await session.flush()


async def test_experiment_idempotency_key_is_unique_per_workspace(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    versions = await _versions(sessionmaker, workspace_id)
    await _rejected(
        sessionmaker,
        _experiment(workspace_id, versions, idempotency_key="k"),
        _experiment(workspace_id, versions, idempotency_key="k"),
    )


async def test_unknown_experiment_status_is_rejected(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    versions = await _versions(sessionmaker, workspace_id)
    await _rejected(sessionmaker, _experiment(workspace_id, versions, status="queued"))


async def test_row_cannot_point_into_another_workspace(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    other = models.Workspace(id=new_id("ws"), name="other")
    async with sessionmaker.begin() as session:
        session.add(other)
    versions = await _versions(sessionmaker, workspace_id)
    await _rejected(sessionmaker, _experiment(other.id, versions))


async def test_the_same_work_cannot_be_pending_twice(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    await _rejected(sessionmaker, _job(workspace_id), _job(workspace_id, status="running"))
    # Once the first is finished, the same work may be queued again.
    async with sessionmaker.begin() as session:
        session.add(_job(workspace_id, status="done"))
        await session.flush()
        session.add(_job(workspace_id))


async def test_unknown_job_status_is_rejected(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    await _rejected(sessionmaker, _job(workspace_id, status="paused"))
