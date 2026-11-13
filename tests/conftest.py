import asyncio
import base64
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

import httpx
import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.auth.api_tokens import create_api_token
from app.auth.principal import Principal
from app.auth.users import create_user
from app.auth.workspaces import create_workspace
from app.db import models
from app.db.budgets import BudgetLimits, create_budget
from app.db.datasets import CaseData, save_version
from app.db.engine import create_engine, create_sessionmaker
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.db.traces import agent_version
from app.settings import Settings

# Tests set what they need themselves, so a developer's own .env cannot change their outcome.
Settings.model_config["env_file"] = None


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


class LivePlatform:
    """The real API and a worker, each in its own process, on a fresh database. SDK tests
    use it to stop and start the platform under a running Eval."""

    def __init__(self, database_url: str) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._env = {
            **os.environ,
            "DATABASE_URL": database_url,
            "LOG_FORMAT": "text",
            "WORKER_IDLE_SECONDS": "0.1",
        }
        self._processes: list[subprocess.Popen[bytes]] = []

    def start(self) -> None:
        api = [
            *("-m", "uvicorn", "app.api.main:create_app", "--factory"),
            *("--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"),
        ]
        for arguments in (api, ["-m", "app.jobs.worker"]):
            self._processes.append(
                subprocess.Popen([sys.executable, *arguments], env=self._env)  # noqa: S603
            )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.url}/health/ready", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise AssertionError("the API did not start")

    def stop(self) -> None:
        for process in self._processes:
            process.terminate()
        for process in self._processes:
            process.wait(10)
        self._processes = []


@pytest.fixture
def live_platform(fresh_database_url: str) -> Iterator[LivePlatform]:
    platform = LivePlatform(fresh_database_url)
    platform.start()
    yield platform
    platform.stop()


@pytest.fixture
async def member_token(fresh_sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[str, str]:
    """A member API token for a fresh workspace on the live platform's database."""
    async with fresh_sessionmaker.begin() as session:
        user = await create_user(session, f"{new_id('u')}@example.com", "a long enough password")
        workspace = await create_workspace(session, user, "team")
        owner = Principal(
            actor_type=models.ActorType.USER,
            actor_id=user.id,
            user_id=user.id,
            workspace_id=workspace.id,
            role=models.Role.OWNER,
        )
        _, secret = await create_api_token(
            WorkspaceScope(session, workspace.id), owner, "sdk", models.Role.MEMBER
        )
    return secret, workspace.id
