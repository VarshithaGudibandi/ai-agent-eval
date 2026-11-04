import asyncio
import base64
import os
import secrets
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.agents.contract import RunLimits
from app.db import models
from app.db.budgets import BudgetLimits
from app.db.content import create_agent_config, create_dataset_version, load_task_file
from app.db.engine import create_engine, create_sessionmaker
from app.db.experiments import complete_if_done, create_experiment, start_attempt
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.environments.registry import default_registry
from app.evaluators.registry import default_evaluators
from app.execution.attempt_runner import Platform, run_attempt
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
                category="c",
                difficulty="easy",
                review={},
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
                split="dev",
                final=False,
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
        session.add(
            models.Budget(
                id=new_id("bud"),
                workspace_id=workspace_id,
                experiment_id=ids["exp"],
                currency="USD",
            )
        )
        await session.flush()
        session.add(attempt)
    return attempt


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


TASK_FILE = (
    Path(__file__).parents[1] / "datasets/support_retail/tasks/refund_damaged_headphones.json"
)

MakeExperiment = Callable[[BudgetLimits], Awaitable[models.Experiment]]


@pytest.fixture
def make_experiment(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> MakeExperiment:
    async def make(limits: BudgetLimits) -> models.Experiment:
        async with sessionmaker.begin() as session:
            scope = WorkspaceScope(session, workspace_id)
            version = await create_dataset_version(scope, "d", [load_task_file(TASK_FILE)])
            config = await create_agent_config(
                scope, "c", models.AdapterType.SCRIPTED, {}, RunLimits()
            )
            experiment = await create_experiment(
                scope, version.id, [config.id], [], mode=models.ExperimentMode.DEMO, budget=limits
            )
            experiment.status = models.ExperimentStatus.RUNNING
            return experiment

    return make


GOOD_SCRIPT = {
    "steps": [
        {"tool": "get_order", "arguments": {"order_id": "ord_1001"}},
        {"tool": "get_return_status", "arguments": {"order_id": "ord_1001"}},
        {
            "tool": "request_refund_approval",
            "arguments": {"order_id": "ord_1001", "amount_minor": 24999, "reason": "damaged"},
        },
        {
            "tool": "issue_refund",
            "arguments": {"order_id": "ord_1001", "amount_minor": 24999, "approval_id": "apr_0001"},
        },
    ],
    "final_response": "Your return passed inspection, so I refunded the full $249.99.",
}
# Skips approval, so the store blocks the refund: an unsafe attempt, not an executed one. The
# malformed lookup first is a call that fails schema validation.
UNAPPROVED_SCRIPT = {
    "steps": [
        {"tool": "get_order", "arguments": {"order": "ord_1001"}},
        {"tool": "get_order", "arguments": {"order_id": "ord_1001"}},
        {
            "tool": "issue_refund",
            "arguments": {"order_id": "ord_1001", "amount_minor": 24999, "approval_id": "apr_9999"},
        },
    ],
    "final_response": "Your refund of $249.99 is on its way.",
}


@dataclass
class DemoExperiment:
    experiment_id: str
    config_ids: list[str]


RunDemo = Callable[..., Awaitable[DemoExperiment]]


@pytest.fixture
def run_demo() -> RunDemo:
    """Run a demo experiment on the refund task through the real gateway and evaluators."""

    async def run(
        sessionmaker: async_sessionmaker[AsyncSession],
        workspace_id: str,
        scripts: list[dict[str, Any]],
        repetitions: int = 1,
    ) -> DemoExperiment:
        platform = Platform(sessionmaker, default_registry(), default_evaluators())
        async with sessionmaker.begin() as session:
            scope = WorkspaceScope(session, workspace_id)
            version = await create_dataset_version(scope, "d", [load_task_file(TASK_FILE)])
            configs = [
                await create_agent_config(
                    scope, f"c{i}", models.AdapterType.SCRIPTED, {"script": s}, RunLimits()
                )
                for i, s in enumerate(scripts)
            ]
            experiment = await create_experiment(
                scope,
                version.id,
                [c.id for c in configs],
                platform.evaluators,
                mode=models.ExperimentMode.DEMO,
                repetitions=repetitions,
            )
            experiment.status = models.ExperimentStatus.RUNNING
            runs = await scope.all(
                scope.query(models.Run).where(models.Run.experiment_id == experiment.id)
            )
        for run in runs:
            async with sessionmaker.begin() as session:
                attempt = await start_attempt(WorkspaceScope(session, workspace_id), run.id)
            await run_attempt(platform, workspace_id, attempt.id)
        async with sessionmaker.begin() as session:
            await complete_if_done(WorkspaceScope(session, workspace_id), experiment.id)
        return DemoExperiment(experiment.id, [c.id for c in configs])

    return run
