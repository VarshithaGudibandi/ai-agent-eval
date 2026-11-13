from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from sqlalchemy import update

from app.db import models
from app.scoring.judge_schedules import start_due_batches
from tests.api.conftest import SignIn, viewer_caller
from tests.api.seeding import add_dataset, run_experiment
from tests.ingest.sample_traces import CONFIG, support_trace


async def test_settings_show_owner_tools_only_to_owners(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    page = await owner.get("/settings")
    assert page.status_code == 200
    assert "API tokens" in page.text and "Provider keys" in page.text
    assert str(owner.email) in page.text
    invite = await owner.post(
        f"/api/v1/workspaces/{owner.workspace_id}/invites", json={"role": "viewer"}
    )
    viewer_client = clients()
    accepted = await viewer_client.post(
        "/api/v1/invites/accept",
        json={
            "token": invite.json()["token"],
            "email": "viewer@example.com",
            "password": "a long enough password",
        },
    )
    assert accepted.status_code == 200
    await viewer_client.post(
        "/api/v1/auth/login",
        json={
            "email": "viewer@example.com",
            "password": "a long enough password",
            "workspace_id": owner.workspace_id,
        },
    )
    seen = await viewer_client.get("/settings")
    assert seen.status_code == 200
    assert "API tokens" not in seen.text and "Provider keys" not in seen.text


async def test_without_agents_only_the_workspace_settings_show(new_owner: SignIn) -> None:
    owner = await new_owner()
    page = (await owner.get("/settings")).text
    assert "Budgets" in page and "No agents yet." in page


async def test_an_agents_spec_gates_schedule_and_versions_can_be_changed(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    dataset_id = await add_dataset(owner)
    experiment = await run_experiment(owner, app, dataset_id, CONFIG, crash=set())
    [agent] = (await owner.get("/api/v1/agents")).json()
    await owner.put(f"/api/v1/agents/{agent['id']}/gates", json={"max_cost_increase": None})
    page = (await owner.get("/settings")).text
    assert f"experiment <code>{experiment['id'][:12]}…</code>" in page
    assert "Eval spec · v1" in page and "run_completed" in page
    assert 'name="scorers" data-type="json"' in page and "&#34;turn_limit&#34;" in page
    assert 'name="max_success_drop"' in page and 'value="5.0"' in page
    # No cost limit shows as an empty field, which sends null back.
    assert 'data-empty="null" value=""' in page
    assert 'name="weekday" data-type="int"' in page and ">every day</option>" in page
    assert ">Mark shipped</button>" in page and 'data-method="PATCH"' in page


async def test_viewers_see_the_settings_but_cannot_change_them(
    new_owner: SignIn, app: FastAPI, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    viewer = await viewer_caller(owner, clients())
    page = (await viewer.get("/settings")).text
    assert "Viewers can read these settings but not change them." in page
    assert "Save gates" not in page and "Save a new version" not in page
    assert "Mark shipped" not in page and "disabled" in page


async def test_a_schedule_that_could_not_start_says_why(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    [agent] = (await owner.get("/api/v1/agents")).json()
    await owner.put(f"/api/v1/agents/{agent['id']}/judge-schedule", json={"hour": 9})
    async with app.state.sessionmaker.begin() as session:
        await session.execute(
            update(models.JudgeSchedule)
            .where(models.JudgeSchedule.agent_id == agent["id"])
            .values(next_run_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
    await start_due_batches(app.state.sessionmaker)
    page = (await owner.get(f"/settings?agent={agent['id']}")).text
    assert "The last scheduled batch could not start" in page and "marked paid" in page


async def test_another_workspaces_agent_cannot_be_chosen(new_owner: SignIn) -> None:
    owner, other = await new_owner(), await new_owner()
    await other.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    [agent] = (await other.get("/api/v1/agents")).json()
    assert (await owner.get(f"/settings?agent={agent['id']}")).status_code == 404
