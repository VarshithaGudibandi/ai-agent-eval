from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.settings import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"timeout": settings.db_connect_timeout_seconds},
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # Rows stay readable after commit, so code can hand them on without reloading.
    return async_sessionmaker(engine, expire_on_commit=False)
