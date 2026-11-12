import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.ids import new_id


async def _dataset_version(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> str:
    dataset_id, version_id = new_id("ds"), new_id("dsv")
    async with sessionmaker.begin() as session:
        session.add(models.Dataset(id=dataset_id, workspace_id=workspace_id, name=dataset_id))
        await session.flush()
        session.add(
            models.DatasetVersion(
                id=version_id,
                workspace_id=workspace_id,
                dataset_id=dataset_id,
                version=1,
                content_hash="h",
            )
        )
    return version_id


def _experiment(workspace_id: str, dataset_version_id: str, **overrides: object) -> models.Base:
    values: dict[str, object] = {
        "id": new_id("exp"),
        "workspace_id": workspace_id,
        "dataset_version_id": dataset_version_id,
        "mode": "real",
        "split": "dev",
        "final": False,
        "status": "queued",
        "repetitions": 1,
        "manifest": {},
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
    version = await _dataset_version(sessionmaker, workspace_id)
    await _rejected(
        sessionmaker,
        _experiment(workspace_id, version, idempotency_key="k"),
        _experiment(workspace_id, version, idempotency_key="k"),
    )


async def test_unknown_experiment_mode_is_rejected(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    version = await _dataset_version(sessionmaker, workspace_id)
    await _rejected(sessionmaker, _experiment(workspace_id, version, mode="other"))


async def test_row_cannot_point_into_another_workspace(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    other = models.Workspace(id=new_id("ws"), name="other")
    async with sessionmaker.begin() as session:
        session.add(other)
    version = await _dataset_version(sessionmaker, workspace_id)
    await _rejected(sessionmaker, _experiment(other.id, version))


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
