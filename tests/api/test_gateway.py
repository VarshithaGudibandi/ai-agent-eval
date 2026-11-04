from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select, update

from app.db import models
from app.db.jobs import cancel_work, claim_next
from app.db.run_tokens import issue_run_token
from app.db.scope import WorkspaceScope
from app.environments.state_store import EnvStateStore
from app.environments.support_retail.environment import SupportRetail
from tests.api.conftest import SCRIPT, TASK, Caller, SignIn
from tests.environments.support_retail.fixtures import FIXTURE


@dataclass
class Live:
    attempt_id: str
    experiment_id: str
    workspace_id: str
    token: str

    def url(self, tool: str, attempt_id: str | None = None) -> str:
        return f"/gw/v1/attempts/{attempt_id or self.attempt_id}/tools/{tool}"

    @property
    def auth(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}"}


async def _live_attempt(owner: Caller, app: FastAPI) -> Live:
    """An attempt a worker has claimed and handed to an external agent."""
    imported = await owner.post("/api/v1/datasets/import", json={"name": "r", "tasks": [TASK]})
    config = await owner.post(
        "/api/v1/agent-configs",
        json={"name": "s", "adapter_type": "scripted", "settings": SCRIPT},
    )
    experiment = await owner.post(
        "/api/v1/experiments",
        json={
            "dataset_version_id": imported.json()["dataset_version_id"],
            "agent_config_ids": [config.json()["id"]],
            "mode": "demo",
        },
    )
    experiment_id = experiment.json()["id"]
    async with app.state.sessionmaker.begin() as session:
        claim = await claim_next(session, "w", 30, experiment_id=experiment_id)
        assert claim is not None
        attempt = await session.get_one(models.Attempt, claim.attempt_id)
        await SupportRetail(EnvStateStore(session, owner.workspace_id)).setup(
            attempt.env_id, FIXTURE
        )
        token = await issue_run_token(
            session, owner.workspace_id, claim.attempt_id, claim.lease_token, 60
        )
    return Live(claim.attempt_id, experiment_id, owner.workspace_id, token)


def _agent(app: FastAPI) -> httpx.AsyncClient:
    # An external agent holds no session or API token: only the run token.
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _events(app: FastAPI, attempt_id: str) -> int:
    async with app.state.sessionmaker() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(models.TraceEvent)
            .where(models.TraceEvent.attempt_id == attempt_id)
        )
    return count or 0


async def test_an_agent_calls_tools_with_its_run_token(new_owner: SignIn, app: FastAPI) -> None:
    live = await _live_attempt(await new_owner(), app)
    async with _agent(app) as agent:
        response = await agent.post(
            live.url("get_order"), json={"order_id": "ord_1001"}, headers=live.auth
        )
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["output"]["total_minor"]) == ("ok", 24999)
    assert await _events(app, live.attempt_id) == 2


async def test_bad_missing_or_misplaced_tokens_are_refused(new_owner: SignIn, app: FastAPI) -> None:
    mine = await _live_attempt(await new_owner(), app)
    theirs = await _live_attempt(await new_owner(), app)
    async with _agent(app) as agent:
        missing = await agent.post(mine.url("get_order"), json={"order_id": "ord_1001"})
        wrong = await agent.post(
            mine.url("get_order"),
            json={"order_id": "ord_1001"},
            headers={"authorization": "Bearer run_guess"},
        )
        # A token is good for its own attempt only, even across workspaces.
        crossed = await agent.post(
            mine.url("get_order", theirs.attempt_id),
            json={"order_id": "ord_1001"},
            headers=mine.auth,
        )
    assert [r.status_code for r in (missing, wrong, crossed)] == [401, 401, 401]
    assert crossed.json() == {"error": "invalid_run_token"}
    assert await _events(app, theirs.attempt_id) == 0


async def test_an_expired_token_is_refused(new_owner: SignIn, app: FastAPI) -> None:
    live = await _live_attempt(await new_owner(), app)
    async with app.state.sessionmaker.begin() as session:
        await session.execute(
            update(models.Attempt)
            .where(models.Attempt.id == live.attempt_id)
            .values(run_token_expires_at=func.now())
        )
    async with _agent(app) as agent:
        response = await agent.post(
            live.url("get_order"), json={"order_id": "ord_1001"}, headers=live.auth
        )
    assert response.status_code == 401


async def test_cancelling_locks_the_agent_out_at_once(new_owner: SignIn, app: FastAPI) -> None:
    live = await _live_attempt(await new_owner(), app)
    async with app.state.sessionmaker.begin() as session:
        await cancel_work(WorkspaceScope(session, live.workspace_id), live.experiment_id)
    async with _agent(app) as agent:
        response = await agent.post(
            live.url("get_order"), json={"order_id": "ord_1001"}, headers=live.auth
        )
    assert (response.status_code, response.json()) == (409, {"error": "attempt_not_live"})
    assert await _events(app, live.attempt_id) == 0


async def test_an_agent_posts_one_final_answer(new_owner: SignIn, app: FastAPI) -> None:
    live = await _live_attempt(await new_owner(), app)
    url = f"/gw/v1/attempts/{live.attempt_id}/result"
    async with _agent(app) as agent:
        first = await agent.post(url, json={"final_response": "Done."}, headers=live.auth)
        second = await agent.post(url, json={"final_response": "Again."}, headers=live.auth)
    assert first.status_code == 200
    assert (second.status_code, second.json()["error"]) == (409, "result_already_posted")
    async with app.state.sessionmaker() as session:
        attempt = await session.get_one(models.Attempt, live.attempt_id)
    assert attempt.external_result == {"final_response": "Done.", "usage": None}
