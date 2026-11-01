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
