from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.db import models
from tests.api.conftest import Caller, SignIn, viewer_caller
from tests.api.seeding import QUESTION, judged_live_run, send_live
from tests.ingest.sample_traces import support_trace


async def _live_page(owner: Caller) -> str:
    [agent] = (await owner.get("/api/v1/agents")).json()
    page = await owner.get(f"/agents/{agent['id']}/live")
    assert page.status_code == 200, page.text
    return page.text


async def test_an_agent_without_recent_live_runs_says_so_in_every_section(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    await send_live(owner, app, "old", datetime.now(UTC) - timedelta(days=40), passed=3, failed=0)
    page = await _live_page(owner)
    assert "No live runs in the last 14 days" in page and "Nothing to show yet" in page
    assert "No alerts" in page and "Nothing to review" in page


async def test_traffic_daily_health_and_an_alert(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    now = datetime.now(UTC)
    await send_live(owner, app, "prev", now - timedelta(days=10), passed=38, failed=2)
    await send_live(owner, app, "last", now - timedelta(days=2), passed=24, failed=16)
    page = await _live_page(owner)
    assert "80 runs, 80 scored (100%)" in page
    assert "60%" in page and "95%" in page  # the two days' success rates
    assert "v1" in page  # each day's version
    assert "Last week: success rate down 35.0 points" in page


async def test_judged_runs_in_the_queue_can_be_labelled(
    new_owner: SignIn,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    clients: Callable[[], httpx.AsyncClient],
) -> None:
    owner = await new_owner()
    run_id = await judged_live_run(owner, app, monkeypatch)
    await owner.post("/api/v1/review-items", json={"run_id": run_id})
    page = await _live_page(owner)
    assert QUESTION["text"] in page
    assert 'name="labels.0.answer" value="yes"' in page
    assert 'name="labels.0.question_id" value="says_shipped"' in page
    # The judge's own answer and quote are not shown, so they cannot sway the person
    # labelling. (The reply itself does say "shipped yesterday".)
    queue = page.split('id="l4"')[1]
    assert "“shipped yesterday”" not in queue and "not trusted yet" not in queue
    viewer = await viewer_caller(owner, clients())
    [agent] = (await owner.get("/api/v1/agents")).json()
    seen = (await viewer.get(f"/agents/{agent['id']}/live")).text
    assert QUESTION["text"] in seen and "Save labels" not in seen


async def test_a_run_never_judged_has_nothing_to_label(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    async with app.state.sessionmaker() as session:
        run_id = await session.scalar(
            select(models.Run.id).where(models.Run.workspace_id == owner.workspace_id)
        )
    await owner.post("/api/v1/review-items", json={"run_id": run_id})
    page = await _live_page(owner)
    assert "nothing to label" in page and "Save labels" not in page


async def test_another_workspaces_agent_has_no_live_page(new_owner: SignIn) -> None:
    owner, other = await new_owner(), await new_owner()
    await other.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    [agent] = (await other.get("/api/v1/agents")).json()
    assert (await owner.get(f"/agents/{agent['id']}/live")).status_code == 404
