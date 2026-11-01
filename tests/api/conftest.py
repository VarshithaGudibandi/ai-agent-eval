import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import CSRF_HEADER
from app.api.main import create_app
from app.auth.users import create_user
from app.auth.workspaces import create_workspace
from app.db.experiments import start_attempt
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.execution.attempt_runner import Platform, run_attempt
from app.settings import Settings

PASSWORD = "a long enough password"


@dataclass
class Caller:
    """One identity calling the API: a signed-in person or an API token."""

    client: httpx.AsyncClient
    workspace_id: str
    user_id: str | None = None
    email: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.client.get(path, headers=self.headers, **kwargs)

    async def post(self, path: str, json: Any = None, **kwargs: Any) -> httpx.Response:
        headers = {**self.headers, **kwargs.pop("headers", {})}
        return await self.client.post(path, json=json, headers=headers, **kwargs)

    async def delete(self, path: str) -> httpx.Response:
        return await self.client.delete(path, headers=self.headers)


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def clients(app: FastAPI) -> AsyncIterator[Callable[[], httpx.AsyncClient]]:
    async with AsyncExitStack() as stack:

        def make() -> httpx.AsyncClient:
            # Each client has its own cookie jar, so each one is a separate browser.
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            )
            stack.push_async_callback(client.aclose)
            return client

        yield make


SignIn = Callable[[], Awaitable[Caller]]


@pytest.fixture
def new_owner(
    sessionmaker: async_sessionmaker[AsyncSession], clients: Callable[[], httpx.AsyncClient]
) -> SignIn:
    """Make a person who owns a fresh workspace and sign them in through the API."""

    async def make() -> Caller:
        email = f"{new_id('u')}@example.com"
        async with sessionmaker.begin() as session:
            user = await create_user(session, email, PASSWORD)
            workspace = await create_workspace(session, user, "team")
        return await sign_in(clients(), email, workspace.id)

    return make


async def sign_in(client: httpx.AsyncClient, email: str, workspace_id: str | None) -> Caller:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD, "workspace_id": workspace_id},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return Caller(
        client=client,
        workspace_id=body["workspace_id"],
        user_id=body["user_id"],
        email=email,
        headers={CSRF_HEADER: body["csrf_token"]},
    )


async def token_caller(owner: Caller, client: httpx.AsyncClient, role: str = "member") -> Caller:
    response = await owner.post("/api/v1/api-tokens", json={"name": "ci", "role": role})
    assert response.status_code == 201, response.text
    return Caller(
        client=client,
        workspace_id=owner.workspace_id,
        headers={"authorization": f"Bearer {response.json()['token']}"},
    )


TASK = json.loads(
    (
        Path(__file__).parents[2] / "datasets/support_retail/tasks/refund_damaged_headphones.json"
    ).read_text()
)
SCRIPT = {
    "script": {
        "steps": [
            {"tool": "get_order", "arguments": {"order_id": "ord_1001"}},
            {
                "tool": "issue_refund",
                "arguments": {"order_id": "ord_1001", "amount_minor": 24999, "reason": "damaged"},
            },
        ],
        "final_response": "Refunded in full.",
    }
}


@dataclass
class Seeded:
    dataset_version_id: str
    agent_config_id: str
    experiment_id: str
    run_id: str
    attempt_id: str


async def seed_and_run(caller: Caller, app: FastAPI) -> Seeded:
    """Create a dataset, a config and a demo experiment through the API, then run it."""
    imported = await caller.post(
        "/api/v1/datasets/import", json={"name": "retail", "tasks": [TASK]}
    )
    assert imported.status_code == 201, imported.text
    config = await caller.post(
        "/api/v1/agent-configs",
        json={"name": "scripted", "adapter_type": "scripted", "settings": SCRIPT},
    )
    assert config.status_code == 201, config.text
    experiment = await caller.post(
        "/api/v1/experiments",
        json={
            "dataset_version_id": imported.json()["dataset_version_id"],
            "agent_config_ids": [config.json()["id"]],
            "mode": "demo",
        },
    )
    assert experiment.status_code == 201, experiment.text
    [run] = (await caller.get(f"/api/v1/experiments/{experiment.json()['id']}/runs")).json()
    platform = Platform(app.state.sessionmaker, app.state.environments, app.state.evaluators)
    async with app.state.sessionmaker.begin() as session:
        attempt = await start_attempt(WorkspaceScope(session, caller.workspace_id), run["id"])
    await run_attempt(platform, caller.workspace_id, attempt.id)
    return Seeded(
        dataset_version_id=imported.json()["dataset_version_id"],
        agent_config_id=config.json()["id"],
        experiment_id=experiment.json()["id"],
        run_id=run["id"],
        attempt_id=attempt.id,
    )
