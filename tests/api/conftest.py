from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import CSRF_HEADER
from app.api.main import create_app
from app.auth.users import create_user
from app.auth.workspaces import create_workspace
from app.db import models
from app.db.ids import new_id
from app.db.jobs import Claim, finish
from app.db.scope import WorkspaceScope
from app.explain.job import EXPLAIN, explain_job
from app.jobs.worker import JobContext
from app.metrics.aggregate_job import AGGREGATE_DAY, aggregate_day_job
from app.scoring.job import SCORE_RUN, score_run_job
from app.scoring.judge_job import JUDGE_RUN, judge_run_job
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

    async def put(self, path: str, json: Any = None) -> httpx.Response:
        return await self.client.put(path, json=json, headers=self.headers)

    async def patch(self, path: str, json: Any = None) -> httpx.Response:
        return await self.client.patch(path, json=json, headers=self.headers)

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


async def viewer_caller(owner: Caller, client: httpx.AsyncClient) -> Caller:
    """A person the owner invited as a viewer, signed in through the API."""
    invite = await owner.post(
        f"/api/v1/workspaces/{owner.workspace_id}/invites", json={"role": "viewer"}
    )
    email = f"{new_id('viewer')}@example.com"
    accepted = await client.post(
        "/api/v1/invites/accept",
        json={"token": invite.json()["token"], "email": email, "password": PASSWORD},
    )
    assert accepted.status_code == 200, accepted.text
    return await sign_in(client, email, owner.workspace_id)


async def add_dataset(app: FastAPI, workspace_id: str, name: str) -> tuple[str, str]:
    """A dataset with one version, straight into the database. Returns both ids."""
    dataset_id, version_id = new_id("ds"), new_id("dsv")
    sessionmaker: async_sessionmaker[AsyncSession] = app.state.sessionmaker
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        scope.add(models.Dataset(id=dataset_id, workspace_id=workspace_id, name=name))
        await scope.flush()
        scope.add(
            models.DatasetVersion(
                id=version_id,
                workspace_id=workspace_id,
                dataset_id=dataset_id,
                version=1,
                content_hash="h",
            )
        )
    return dataset_id, version_id


async def score_now(app: FastAPI, workspace_id: str) -> int:
    """Run this workspace's waiting scoring and judging jobs, as the worker would. Returns
    how many ran.

    The worker claims the oldest job of any workspace, and other tests leave theirs queued,
    so tests take their own workspace's jobs directly.
    """
    sessionmaker: async_sessionmaker[AsyncSession] = app.state.sessionmaker
    handlers = {
        SCORE_RUN: score_run_job,
        JUDGE_RUN: judge_run_job,
        AGGREGATE_DAY: aggregate_day_job,
        EXPLAIN: explain_job,
    }
    done = 0
    while True:
        async with sessionmaker.begin() as session:
            job = await session.scalar(
                select(models.Job)
                .where(
                    models.Job.workspace_id == workspace_id,
                    models.Job.kind.in_(list(handlers)),
                    models.Job.status == models.JobStatus.QUEUED,
                )
                .order_by(models.Job.created_at)
                .limit(1)
                .with_for_update()
            )
            if job is None:
                return done
            job.status, job.lease_token = models.JobStatus.RUNNING, new_id("lease")
            job.lease_expires_at = func.now() + func.make_interval(0, 0, 0, 0, 0, 1)
            job.attempts += 1
            claim = Claim(
                workspace_id, job.id, job.kind, job.subject_id, job.payload, job.lease_token, 1
            )
        await handlers[claim.kind](JobContext(claim, sessionmaker, app.state.settings))
        async with sessionmaker.begin() as session:
            await finish(session, workspace_id, claim.job_id, claim.lease_token)
        done += 1
