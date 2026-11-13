from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI

from app.explain import job as explain_job
from app.settings import Settings
from tests.api.conftest import Caller, SignIn, score_now
from tests.api.seeding import V2, add_dataset, run_experiment, send_live
from tests.explain.scripted_auditor import ScriptedAuditor
from tests.ingest.sample_traces import CONFIG, support_trace

CAUSE = "The new prompt dropped the instruction to look the order up first."


async def _agent_page(owner: Caller) -> str:
    [agent] = (await owner.get("/api/v1/agents")).json()
    page = await owner.get(f"/agents/{agent['id']}")
    assert page.status_code == 200, page.text
    return page.text


async def test_no_agents_shows_how_to_connect_one(new_owner: SignIn) -> None:
    owner = await new_owner()
    page = (await owner.get("/agents")).text
    assert "No agents yet" in page and "agent_eval.init(" in page


async def test_the_list_links_each_agent(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    [agent] = (await owner.get("/api/v1/agents")).json()
    page = (await owner.get("/agents")).text
    assert f'href="/agents/{agent["id"]}"' in page and "support-bot" in page


async def test_one_version_shows_its_own_health_and_nothing_to_compare(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    await run_experiment(owner, app, await add_dataset(owner), CONFIG, crash={0})
    page = await _agent_page(owner)
    assert "the only version tested so far" in page
    assert "nothing to compare with" in page
    assert "92%" in page  # 11 of 12 cases passed both times
    assert "First version" in page and "Only one version so far." in page
    assert "run_completed" in page and "2 of 24 runs failed" in page


async def test_two_versions_show_the_change_and_the_explained_cause(
    new_owner: SignIn, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def provider(api_key: str, _: Settings) -> AsyncIterator[ScriptedAuditor]:
        yield ScriptedAuditor(CAUSE)

    monkeypatch.setattr(explain_job, "judge_provider", provider)
    owner = await new_owner()
    await owner.post(
        "/api/v1/provider-credentials",
        json={"provider": "gemini", "label": "free", "api_key": "AIza" + "E" * 35},
    )
    dataset_id = await add_dataset(owner)
    v1 = await run_experiment(owner, app, dataset_id, CONFIG, crash=set())
    v2 = await run_experiment(owner, app, dataset_id, V2, crash=set(range(6)))
    page = await _agent_page(owner)
    assert "compared with" in page and "likely real" in page and "-50.0 pts" in page
    assert "prompt: 2 sentences removed, 1 added" in page
    assert CAUSE not in page
    body = {"baseline": {"experiment_id": v1["id"]}, "candidate": {"experiment_id": v2["id"]}}
    comparison = (await owner.post("/api/v1/comparisons", json=body)).json()
    await owner.post("/api/v1/reports", json={"comparison_id": comparison["id"]})
    await score_now(app, owner.workspace_id)
    assert CAUSE in await _agent_page(owner)


async def test_a_running_experiment_says_its_numbers_will_move(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    dataset_id = await add_dataset(owner)
    experiment = (
        await owner.post(
            "/api/v1/experiments",
            json={"agent": "support-bot", "config": CONFIG, "dataset_version_id": dataset_id},
        )
    ).json()
    tagged = support_trace(
        "t-1", tags={"experiment": experiment["id"], "case": "case-0", "repetition": 1}
    )
    await owner.post("/api/v1/traces", json={"traces": [tagged]})
    page = await _agent_page(owner)
    assert "Still running: 1 of 12 runs received, 0 scored" in page


async def test_live_only_agents_compare_this_week_with_the_last(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    now = datetime.now(UTC)
    await send_live(owner, app, "old", now - timedelta(days=30), passed=5, failed=0)
    # Traffic older than two weeks is not health right now.
    assert "No runs to measure yet" in await _agent_page(owner)
    await send_live(owner, app, "prev", now - timedelta(days=10), passed=40, failed=0)
    await send_live(owner, app, "last", now - timedelta(days=3), passed=20, failed=20)
    page = await _agent_page(owner)
    assert "live runs, last 7 days compared with the 7 before" in page
    assert "-50.0 pts · likely real" in page
    assert "failed runs in the last 7 days of live runs" in page


async def test_trace_content_is_escaped(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    hostile = {**CONFIG, "prompt": '<script>alert("x")</script> Be careful.'}
    trace = support_trace("t-1", config=hostile)
    trace["spans"][0]["error"] = '"><img src=x onerror=alert(1)>'
    await owner.post("/api/v1/traces", json={"traces": [trace]})
    await score_now(app, owner.workspace_id)
    page = await _agent_page(owner)
    assert "<script>alert" not in page and "&lt;script&gt;alert" in page
    assert "<img src=x" not in page


async def test_another_workspaces_agent_is_a_not_found_page(new_owner: SignIn) -> None:
    owner, other = await new_owner(), await new_owner()
    await other.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    [agent] = (await other.get("/api/v1/agents")).json()
    page = await owner.get(f"/agents/{agent['id']}")
    assert page.status_code == 404
    assert page.headers["content-type"].startswith("text/html")
    assert "Not found" in page.text and "support-bot" not in page.text
