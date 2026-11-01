from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.settings import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"timeout": settings.db_connect_timeout_seconds},
    )
