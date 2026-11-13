from collections.abc import Callable
from datetime import timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.retention import remove_old_text, without_text
from tests.api.conftest import Caller, SignIn, score_now, token_caller
from tests.ingest.sample_traces import support_trace


async def _sent(owner: Caller, app: FastAPI, trace_id: str, days_ago: int) -> str:
    """A scored trace received days_ago. Returns its run id."""
    await owner.post("/api/v1/traces", json={"traces": [support_trace(trace_id)]})
    await score_now(app, owner.workspace_id)
    sessionmaker: async_sessionmaker[AsyncSession] = app.state.sessionmaker
    async with sessionmaker.begin() as session:
        trace = await session.scalar(
            select(models.Trace).where(
                models.Trace.workspace_id == owner.workspace_id,
                models.Trace.trace_id == trace_id,
            )
        )
        assert trace is not None
        trace.received_at = func.now() - timedelta(days=days_ago)
        run_id = await session.scalar(
            select(models.Run.id).where(models.Run.trace_row_id == trace.id)
        )
    return str(run_id)


async def _remove_all(app: FastAPI) -> int:
    removed = 0
    while True:
        async with app.state.sessionmaker.begin() as session:
            batch = await remove_old_text(session)
        removed += batch
        if batch < 200:
            return removed


async def test_text_past_the_limit_goes_and_everything_else_stays(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner, keeper = await new_owner(), await new_owner()
    assert (
        await owner.patch("/api/v1/workspaces/current", json={"retention_days": 7})
    ).status_code == 200
    old = await _sent(owner, app, "old", days_ago=8)
    recent = await _sent(owner, app, "recent", days_ago=6)
    # A workspace with no limit keeps even very old text.
    kept = await _sent(keeper, app, "ancient", days_ago=400)
    before = (await owner.get(f"/api/v1/runs/{old}")).json()

    await _remove_all(app)

    after = (await owner.get(f"/api/v1/runs/{old}")).json()
    assert after["text_removed_at"] is not None
    assert all(s["input"] is None and s["output"] is None for s in after["spans"])
    assert [s["name"] for s in after["spans"]] == [s["name"] for s in before["spans"]]
    assert after["scores"] == before["scores"]
    assert (after["cost_micros"], after["duration_ms"]) == (
        before["cost_micros"],
        before["duration_ms"],
    )
    for caller, run_id in ((owner, recent), (keeper, kept)):
        untouched = (await caller.get(f"/api/v1/runs/{run_id}")).json()
        assert untouched["text_removed_at"] is None
        assert any(s["input"] is not None for s in untouched["spans"])
    async with app.state.sessionmaker() as session:
        payload = await session.scalar(
            select(models.Trace.payload).where(
                models.Trace.workspace_id == owner.workspace_id, models.Trace.trace_id == "old"
            )
        )
    assert payload == without_text(payload) and payload["spans"]
    page = (await owner.get(f"/runs/{old}")).text
    assert "were removed on" in page


async def test_removed_runs_are_left_out_of_rescoring(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    await owner.patch("/api/v1/workspaces/current", json={"retention_days": 30})
    await _sent(owner, app, "old", days_ago=31)
    await _sent(owner, app, "recent", days_ago=1)
    await _remove_all(app)
    [agent] = (await owner.get("/api/v1/agents")).json()
    rescored = (await owner.post(f"/api/v1/agents/{agent['id']}/rescore", json={})).json()
    assert rescored["queued"] + rescored["already_waiting"] == 1


async def test_only_an_owner_sets_retention_and_unset_keeps_text(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    member = await token_caller(owner, clients(), role="member")
    refused = await member.patch("/api/v1/workspaces/current", json={"retention_days": 1})
    assert refused.status_code == 403
    assert (
        await owner.patch("/api/v1/workspaces/current", json={"retention_days": 0})
    ).status_code == 422
    await owner.patch("/api/v1/workspaces/current", json={"retention_days": 14})
    [listed] = (await owner.get("/api/v1/workspaces")).json()
    assert listed["retention_days"] == 14
    cleared = await owner.patch("/api/v1/workspaces/current", json={"retention_days": None})
    assert cleared.json()["retention_days"] is None
