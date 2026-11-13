from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.db import models
from app.scoring import judge_job
from app.settings import Settings
from tests.api.conftest import Caller, SignIn, score_now, viewer_caller
from tests.api.seeding import add_dataset, run_experiment
from tests.ingest.sample_traces import CONFIG, support_trace
from tests.scoring.scripted_judge import AnsweringJudge

CASES = [{"key": "where-is-my-order", "input": {"q": "where?"}, "expected": {"reply": "shipped"}}]


async def _run_id(app: FastAPI, workspace_id: str, trace_id: str) -> str:
    async with app.state.sessionmaker() as session:
        run_id = await session.scalar(
            select(models.Run.id)
            .join(models.Trace, models.Trace.id == models.Run.trace_row_id)
            .where(models.Trace.workspace_id == workspace_id, models.Trace.trace_id == trace_id)
        )
    assert run_id is not None
    return str(run_id)


async def _page(owner: Caller, run_id: str, query: str = "") -> str:
    page = await owner.get(f"/runs/{run_id}{query}")
    assert page.status_code == 200, page.text
    return page.text


async def test_an_experiment_run_shows_its_task_trace_and_failing_step(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    experiment = await run_experiment(
        owner, app, await add_dataset(owner, CASES), CONFIG, crash={0}, cases=CASES
    )
    page = await _page(owner, await _run_id(app, owner.workspace_id, f"{experiment['id']}-0-1"))
    assert "where-is-my-order" in page and "Fail" in page
    assert "Expected · hidden from the agent" in page and "shipped" in page
    assert "1 of 2 · 0 of the others passed" in page
    # The steps in tree order, the root first.
    assert page.index("handle_request") < page.index("plan") < page.index("get_order")
    # With no step asked for, the failing step is shown: here the root, which crashed.
    assert "Error: RuntimeError: forgot the lookup" in page
    assert "run_completed" in page and "Built-in" in page


async def test_a_live_run_has_no_expected_outcome_and_any_step_can_be_opened(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    trace = support_trace("t-1")
    trace["spans"][2]["output"] = {"status": "<script>alert(1)</script>"}
    await owner.post("/api/v1/traces", json={"traces": [trace]})
    run_id = await _run_id(app, owner.workspace_id, "t-1")
    unscored = await _page(owner, run_id)
    assert "Not scored yet" in unscored and "Waiting to be scored" in unscored
    await score_now(app, owner.workspace_id)
    page = await _page(owner, run_id, "?span=t1")
    assert "Expected" not in page and "live · production" in page
    assert 'href="?span=t1#trace" aria-current="true"' in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert" not in page
    model = await _page(owner, run_id, "?span=m1")
    # The planning call: 300 ms to the first token, then 50 tokens in the remaining 500 ms.
    assert "300 ms" in model and "100 tok/s" in model
    # A step that is not in the run falls back to the default.
    assert "handle_request" in await _page(owner, run_id, "?span=nope")


async def test_judged_runs_show_each_answer_and_members_can_send_them_for_review(
    new_owner: SignIn,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    clients: Callable[[], httpx.AsyncClient],
) -> None:
    scripted = AnsweringJudge({"says_shipped": ("yes", "shipped yesterday")})

    @asynccontextmanager
    async def provider(api_key: str, _: Settings) -> AsyncIterator[AnsweringJudge]:
        yield scripted

    monkeypatch.setattr(judge_job, "judge_provider", provider)
    app.state.settings = app.state.settings.model_copy(update={"review_sample_share": 0.0})
    owner = await new_owner()
    await owner.post(
        "/api/v1/provider-credentials",
        json={"provider": "gemini", "label": "j", "api_key": "AIza" + "B" * 35, "paid": True},
    )
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-0")]})
    [agent] = (await owner.get("/api/v1/agents")).json()
    question = {"id": "says_shipped", "text": "Does the reply say the order has shipped?"}
    await owner.put(
        f"/api/v1/agents/{agent['id']}/eval-spec",
        json={
            "scorers": [
                {"key": "support", "check": "checklist", "params": {"questions": [question]}}
            ]
        },
    )
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    await score_now(app, owner.workspace_id)
    window = {"start": "2026-11-01T00:00:00Z", "end": "2026-12-01T00:00:00Z"}
    await owner.post("/api/v1/judge-batches", json={"agent_id": agent["id"], **window})
    await score_now(app, owner.workspace_id)
    run_id = await _run_id(app, owner.workspace_id, "t-1")
    page = await _page(owner, run_id)
    assert "Does the reply say the order has shipped?" in page
    assert "“shipped yesterday”" in page and "not trusted yet" in page
    assert 'data-api="/api/v1/review-items"' in page
    viewer = await viewer_caller(owner, clients())
    seen = await viewer.get(f"/runs/{run_id}")
    assert seen.status_code == 200 and 'data-api="/api/v1/review-items"' not in seen.text


async def test_another_workspaces_run_is_not_found(new_owner: SignIn, app: FastAPI) -> None:
    owner, other = await new_owner(), await new_owner()
    await other.post("/api/v1/traces", json={"traces": [support_trace("secret-trace")]})
    page = await owner.get(f"/runs/{await _run_id(app, other.workspace_id, 'secret-trace')}")
    assert page.status_code == 404 and "secret-trace" not in page.text
