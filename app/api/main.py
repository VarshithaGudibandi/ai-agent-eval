from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import (
    agent_configs,
    api_tokens,
    auth,
    comparisons,
    datasets,
    experiments,
    gateway,
    health,
    prices,
    provider_credentials,
    runs,
    signing_secret,
    workspaces,
)
from app.api.errors import register_error_handlers
from app.db.engine import create_engine, create_sessionmaker
from app.environments.registry import default_registry
from app.evaluators.registry import default_evaluators
from app.gateway.tool_gateway import ToolGateway
from app.providers.registry import default_providers
from app.settings import Settings, get_settings
from app.structured_logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, settings.log_format)
        app.state.engine = create_engine(settings)
        app.state.sessionmaker = create_sessionmaker(app.state.engine)
        app.state.gateway = ToolGateway(app.state.sessionmaker, app.state.environments)
        try:
            yield
        finally:
            await app.state.engine.dispose()

    app = FastAPI(title="AI Agent Eval", lifespan=lifespan)
    app.state.settings = settings
    app.state.environments = default_registry()
    app.state.evaluators = default_evaluators()
    app.state.providers = default_providers()
    register_error_handlers(app)
    for module in (
        health,
        auth,
        workspaces,
        api_tokens,
        provider_credentials,
        prices,
        datasets,
        agent_configs,
        experiments,
        runs,
        comparisons,
        signing_secret,
        gateway,
    ):
        app.include_router(module.router)
    return app
