from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db import models
from app.db.engine import create_engine
from app.db.ids import new_id
from app.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(log_format="text")


@pytest.fixture
def unreachable_settings() -> Settings:
    # Port 1 on loopback refuses immediately, so these tests stay fast.
    return Settings(
        database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
        db_connect_timeout_seconds=1,
        worker_idle_seconds=0.05,
        log_format="text",
    )


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(settings)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def workspace_id(sessionmaker: async_sessionmaker[AsyncSession]) -> str:
    # Each test gets its own workspace, so tests never see each other's rows.
    workspace = models.Workspace(id=new_id("ws"), name="test")
    async with sessionmaker.begin() as session:
        session.add(workspace)
    return workspace.id


@pytest.fixture
async def attempt(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> models.Attempt:
    """A queued attempt with the rows it depends on, for tests below the runner."""
    ids = {name: new_id(name) for name in ("ds", "dsv", "task", "cfg", "exp", "run")}
    attempt = models.Attempt(
        id=new_id("att"),
        workspace_id=workspace_id,
        run_id=ids["run"],
        attempt_number=1,
        env_id=new_id("env"),
        execution_status="queued",
    )
    async with sessionmaker.begin() as session:
        session.add(models.Dataset(id=ids["ds"], workspace_id=workspace_id, name=ids["ds"]))
        session.add(
            models.DatasetVersion(
                id=ids["dsv"],
                workspace_id=workspace_id,
                dataset_id=ids["ds"],
                version=1,
                content_hash="h",
            )
        )
        session.add(
            models.TaskVersion(
                id=ids["task"],
                workspace_id=workspace_id,
                dataset_version_id=ids["dsv"],
                task_key="t",
                environment="support-retail@1",
                scenario_family="f",
                split="dev",
                agent_input={},
                fixture={},
                evaluator_spec={},
                limits={},
                content_hash="h",
            )
        )
        session.add(
            models.AgentConfig(
                id=ids["cfg"],
                workspace_id=workspace_id,
                name="c",
                adapter_type="scripted",
                settings={},
                limits={},
                config_hash="h",
            )
        )
        session.add(
            models.Experiment(
                id=ids["exp"],
                workspace_id=workspace_id,
                dataset_version_id=ids["dsv"],
                mode="real",
                status="queued",
                repetitions=1,
                manifest={},
            )
        )
        await session.flush()
        session.add(
            models.Run(
                id=ids["run"],
                workspace_id=workspace_id,
                experiment_id=ids["exp"],
                agent_config_id=ids["cfg"],
                task_version_id=ids["task"],
                repetition=1,
                execution_status="queued",
                evaluation_status="pending",
            )
        )
        await session.flush()
        session.add(attempt)
    return attempt
