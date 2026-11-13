from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update

from app.db import models
from app.explain import job as explain_job
from app.settings import Settings
from tests.api.conftest import Caller, SignIn, score_now, viewer_caller
from tests.api.seeding import V2, add_dataset, run_experiment, send_live
from tests.explain.scripted_auditor import ScriptedAuditor
from tests.ingest.sample_traces import CONFIG

CAUSE = "The new prompt dropped the instruction to look the order up first."


async def _held(owner: Caller, app: FastAPI) -> str:
    dataset_id = await add_dataset(owner)
    v1 = await run_experiment(owner, app, dataset_id, CONFIG, crash=set())
    v2 = await run_experiment(owner, app, dataset_id, V2, crash=set(range(6)))
    body = {"baseline": {"experiment_id": v1["id"]}, "candidate": {"experiment_id": v2["id"]}}
    return str((await owner.post("/api/v1/comparisons", json=body)).json()["id"])


async def test_the_form_needs_two_experiments_and_picks_the_baseline(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    assert "No agents yet" in (await owner.get("/compare")).text
    dataset_id = await add_dataset(owner)
    v1 = await run_experiment(owner, app, dataset_id, CONFIG, crash=set())
    assert "Only one experiment so far" in (await owner.get("/compare")).text
    v2 = await run_experiment(owner, app, dataset_id, V2, crash=set())
    page = (await owner.get("/compare")).text
    assert 'name="baseline.experiment_id"' in page and 'name="candidate.experiment_id"' in page
    assert f'<option value="{v1["id"]}" selected>' in page
    assert f'<option value="{v2["id"]}" selected>' in page
    assert 'name="baseline.start" data-type="date"' in page


async def test_a_held_comparison_shows_the_change_the_numbers_and_the_cases(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    page = (await owner.get(f"/comparisons/{await _held(owner, app)}")).text
    assert "Hold" in page and "Hold this version back." in page
    assert "prompt: 2 sentences removed, 1 added" in page
    assert "-50.0 pts" in page and "likely real" in page
    assert "6 worse · 0 better · 6 same" in page
    assert page.index("case-0") < page.index("case-11")
    assert "success rate down 50.0 points" in page
    assert ">Explain</button>" in page


async def test_the_report_is_polled_while_written_then_shown_with_its_steps(
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
    comparison_id = await _held(owner, app)
    await owner.post("/api/v1/reports", json={"comparison_id": comparison_id})
    queued = (await owner.get(f"/comparisons/{comparison_id}")).text
    assert 'data-poll="3000"' in queued and "Writing the report" in queued
    await score_now(app, owner.workspace_id)
    done = (await owner.get(f"/comparisons/{comparison_id}")).text
    assert "data-poll" not in done
    assert CAUSE in done and "high confidence" in done
    assert "?span=" in done and "see step" in done


async def test_a_report_with_no_key_or_that_failed_says_why(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    comparison_id = await _held(owner, app)
    report = (await owner.post("/api/v1/reports", json={"comparison_id": comparison_id})).json()
    await score_now(app, owner.workspace_id)
    assert "No cause: no Gemini key" in (await owner.get(f"/comparisons/{comparison_id}")).text
    async with app.state.sessionmaker.begin() as session:
        await session.execute(
            update(models.RegressionReport)
            .where(models.RegressionReport.id == report["id"])
            .values(status="failed", error="the budget ran out", report=None)
        )
    failed = (await owner.get(f"/comparisons/{comparison_id}")).text
    assert "could not be written: the budget ran out" in failed and "Try again" in failed


async def test_nothing_worse_needs_no_explaining(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    dataset_id = await add_dataset(owner)
    v1 = await run_experiment(owner, app, dataset_id, CONFIG, crash=set())
    v2 = await run_experiment(owner, app, dataset_id, V2, crash=set())
    body = {"baseline": {"experiment_id": v1["id"]}, "candidate": {"experiment_id": v2["id"]}}
    found = (await owner.post("/api/v1/comparisons", json=body)).json()
    page = (await owner.get(f"/comparisons/{found['id']}")).text
    assert "Fine to ship." in page and "nothing to explain" in page and ">Explain<" not in page


async def test_live_windows_are_not_paired(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    now = datetime.now(UTC)
    await send_live(owner, app, "prev", now - timedelta(days=10), passed=5, failed=0)
    await send_live(owner, app, "last", now - timedelta(days=3), passed=5, failed=0)
    [agent] = (await owner.get("/api/v1/agents")).json()

    def window(start: datetime, end: datetime) -> dict[str, str]:
        return {"agent_id": agent["id"], "start": start.isoformat(), "end": end.isoformat()}

    body = {
        "baseline": window(now - timedelta(days=14), now - timedelta(days=7)),
        "candidate": window(now - timedelta(days=7), now),
    }
    found = (await owner.post("/api/v1/comparisons", json=body)).json()
    page = (await owner.get(f"/comparisons/{found['id']}")).text
    assert "no, live runs" in page and "Not paired" in page
    assert "all versions · live runs" in page and "Not enough to decide" in page


async def test_viewers_can_read_but_not_start_or_explain(
    new_owner: SignIn, app: FastAPI, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    comparison_id = await _held(owner, app)
    viewer = await viewer_caller(owner, clients())
    form = (await viewer.get("/compare")).text
    assert "Viewers can read comparisons but not start them." in form
    assert ">Compare</button>" not in form
    page = (await viewer.get(f"/comparisons/{comparison_id}")).text
    assert "Hold this version back." in page and ">Explain</button>" not in page


async def test_another_workspaces_comparison_is_not_found(new_owner: SignIn, app: FastAPI) -> None:
    owner, other = await new_owner(), await new_owner()
    comparison_id = await _held(other, app)
    page = await owner.get(f"/comparisons/{comparison_id}")
    assert page.status_code == 404 and "case-0" not in page.text
    assert (await owner.get("/compare?agent=agent_nope")).status_code == 404
