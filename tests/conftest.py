import asyncio
import base64
import os
import secrets
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db import models
from app.db.budgets import BudgetLimits, create_budget
from app.db.datasets import CaseData, save_version
from app.db.engine import create_engine, create_sessionmaker
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.db.traces import agent_version
from app.settings import Settings


@pytest.fixture
def settings() -> Settings:
    master_key = base64.b64encode(secrets.token_bytes(32)).decode()
    return Settings(log_format="text", app_master_key=master_key)


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
    return create_sessionmaker(engine)


@pytest.fixture
async def workspace_id(sessionmaker: async_sessionmaker[AsyncSession]) -> str:
    # Each test gets its own workspace, so tests never see each other's rows.
    workspace = models.Workspace(id=new_id("ws"), name="test")
    async with sessionmaker.begin() as session:
        session.add(workspace)
    return workspace.id


@pytest.fixture
async def fresh_database_url(settings: Settings) -> AsyncIterator[str]:
    """A brand-new, migrated database, for tests that need an install with no users yet."""
    name = f"test_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(settings.database_url).set(database=name).render_as_string(hide_password=False)
    try:
        migrate = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "alembic",
            "upgrade",
            "head",
            env={**os.environ, "DATABASE_URL": url},
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, errors = await migrate.communicate()
        assert migrate.returncode == 0, errors.decode()
        yield url
    finally:
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.fixture
async def fresh_sessionmaker(
    settings: Settings, fresh_database_url: str
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(settings.model_copy(update={"database_url": fresh_database_url}))
    yield create_sessionmaker(engine)
    await engine.dispose()


MakeExperiment = Callable[[BudgetLimits], Awaitable[models.Experiment]]


@pytest.fixture
def make_experiment(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> MakeExperiment:
    """A running experiment with its budget, for one agent version over one dataset version."""

    async def make(limits: BudgetLimits) -> models.Experiment:
        async with sessionmaker.begin() as session:
            scope = WorkspaceScope(session, workspace_id)
            version, _ = await save_version(scope, new_id("data"), [CaseData("c1", "hi")])
            agent = await agent_version(scope, "bot", new_id("f"), {}, None)
            experiment = models.Experiment(
                id=new_id("exp"),
                workspace_id=workspace_id,
                agent_version_id=agent.id,
                dataset_version_id=version.id,
                status=models.ExperimentStatus.RUNNING,
                repetitions=1,
            )
            scope.add(experiment)
            await scope.flush()
            await create_budget(scope, experiment.id, limits)
        return experiment

    return make
