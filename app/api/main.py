from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
from app.db.engine import create_engine
from app.settings import Settings, get_settings
from app.structured_logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, settings.log_format)
        app.state.engine = create_engine(settings)
        try:
            yield
        finally:
            await app.state.engine.dispose()

    app = FastAPI(title="AI Agent Eval", lifespan=lifespan)
    app.include_router(health.router)
    return app
