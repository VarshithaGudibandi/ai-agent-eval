from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.engine import create_engine
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
