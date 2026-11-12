from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import (
    agents,
    api_tokens,
    auth,
    datasets,
    experiments,
    feedback,
    health,
    prices,
    provider_credentials,
    reviews,
    runs,
    traces,
    workspaces,
)
from app.api.errors import register_error_handlers
from app.db.engine import create_engine, create_sessionmaker
from app.settings import Settings, get_settings
from app.structured_logging import configure_logging
from app.web.pages import STATIC_DIR, register_pages


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, settings.log_format)
        app.state.engine = create_engine(settings)
        app.state.sessionmaker = create_sessionmaker(app.state.engine)
        try:
            yield
        finally:
            await app.state.engine.dispose()

    app = FastAPI(title="AI Agent Eval", lifespan=lifespan)
    app.state.settings = settings
    register_error_handlers(app)
    for module in (
        health,
        auth,
        workspaces,
        api_tokens,
        provider_credentials,
        prices,
        datasets,
        experiments,
        traces,
        feedback,
        agents,
        runs,
        reviews,
    ):
        app.include_router(module.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    register_pages(app)
    return app
