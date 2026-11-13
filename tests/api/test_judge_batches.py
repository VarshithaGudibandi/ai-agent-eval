from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.db import models
from app.scoring import judge_job
from app.scoring.judge_schedules import start_due_batches
from app.settings import Settings
from tests.api.conftest import Caller, SignIn, score_now
from tests.ingest.sample_traces import support_trace
from tests.scoring.scripted_judge import AnsweringJudge

QUESTIONS = [{"id": "says_shipped", "text": "Does the reply say the order has shipped?"}]
SPEC = {
    "scorers": [
        {"key": "ran", "check": "run_completed"},
        {"key": "support", "check": "checklist", "params": {"questions": QUESTIONS}},
    ]
}
WINDOW = {"start": "2026-11-01T00:00:00Z", "end": "2026-12-01T00:00:00Z"}


@pytest.fixture
def judge(monkeypatch: pytest.MonkeyPatch, app: FastAPI) -> AnsweringJudge:
    scripted = AnsweringJudge({"says_shipped": ("yes", "shipped yesterday")})

    @asynccontextmanager
    async def provider(api_key: str, _: Settings) -> AsyncIterator[AnsweringJudge]:
        yield scripted

    monkeypatch.setattr(judge_job, "judge_provider", provider)
    app.state.settings = app.state.settings.model_copy(update={"review_sample_share": 0.0})
    return scripted


async def _live(owner: Caller, app: FastAPI, paid: bool) -> str:
    await owner.post(
        "/api/v1/provider-credentials",
        json={"provider": "gemini", "label": "j", "api_key": "AIza" + "B" * 35, "paid": paid},
    )
    # One trace makes the agent; the judge spec is then in force when the others arrive.
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    [agent] = (await owner.get("/api/v1/agents")).json()
    await owner.put(f"/api/v1/agents/{agent['id']}/eval-spec", json=SPEC)
    staging = support_trace("t-staging", tags={"environment": "staging"})
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-2"), staging]})
    await score_now(app, owner.workspace_id)
    return str(agent["id"])


async def test_a_batch_judges_only_the_stored_live_runs_asked_for(
    new_owner: SignIn, app: FastAPI, judge: AnsweringJudge
) -> None:
    owner = await new_owner()
    agent_id = await _live(owner, app, paid=True)
    # Rules scored every live run, and no judging was even queued.
    async with app.state.sessionmaker() as session:
        queued = list(
            await session.scalars(
                select(models.Job.kind).where(models.Job.workspace_id == owner.workspace_id)
            )
        )
    assert "judge_run" not in queued and judge.calls == 0
    body = {"agent_id": agent_id, **WINDOW, "tags": {"environment": "production"}, "limit": 10}
    created = await owner.post("/api/v1/judge-batches", json=body)
    assert created.status_code == 201, created.text
    batch = created.json()
    assert (batch["runs"], batch["judged"], batch["status"], batch["trigger"]) == (
        2,
        0,
        "running",
        "manual",
    )
    await score_now(app, owner.workspace_id)
    done = (await owner.get(f"/api/v1/judge-batches/{batch['id']}")).json()
    assert (done["judged"], done["status"]) == (2, "finished")
    assert judge.calls == 2 and done["health"]["runs_scored"] == 2
    async with app.state.sessionmaker() as session:
        budget = await session.scalar(
            select(models.Budget).where(models.Budget.judge_batch_id == batch["id"])
        )
        answered = list(
            await session.scalars(
                select(models.JudgeAnswer.run_id).where(
                    models.JudgeAnswer.workspace_id == owner.workspace_id
                )
            )
        )
    # Room for the one judge to ask three times on each run, and no more.
    assert budget is not None and (budget.requests_limit, budget.requests_used) == (6, 2)
    assert len(answered) == 2


async def test_live_judging_needs_a_paid_key_and_a_judge(
    new_owner: SignIn, app: FastAPI, judge: AnsweringJudge
) -> None:
    owner = await new_owner()
    agent_id = await _live(owner, app, paid=False)
    refused = await owner.post("/api/v1/judge-batches", json={"agent_id": agent_id, **WINDOW})
    assert refused.status_code == 409 and "marked paid" in refused.json()["message"]
    await owner.post(
        "/api/v1/provider-credentials",
        json={"provider": "gemini", "label": "p", "api_key": "AIza" + "C" * 35, "paid": True},
    )
    await owner.put(
        f"/api/v1/agents/{agent_id}/eval-spec",
        json={"scorers": [{"key": "ran", "check": "run_completed"}]},
    )
    no_judges = await owner.post("/api/v1/judge-batches", json={"agent_id": agent_id, **WINDOW})
    assert no_judges.status_code == 409 and "no judges" in no_judges.json()["message"]


async def test_two_batches_can_be_compared(
    new_owner: SignIn, app: FastAPI, judge: AnsweringJudge
) -> None:
    owner = await new_owner()
    agent_id = await _live(owner, app, paid=True)
    ids = []
    for _ in range(2):
        batch = await owner.post("/api/v1/judge-batches", json={"agent_id": agent_id, **WINDOW})
        ids.append(batch.json()["id"])
        await score_now(app, owner.workspace_id)
    body = {"baseline": {"judge_batch_id": ids[0]}, "candidate": {"judge_batch_id": ids[1]}}
    result = (await owner.post("/api/v1/comparisons", json=body)).json()["result"]
    assert result["paired"] is False and result["candidate"]["runs"] == 3


async def test_a_schedule_starts_batches_when_due_and_says_why_it_could_not(
    new_owner: SignIn, app: FastAPI, judge: AnsweringJudge
) -> None:
    owner = await new_owner()
    agent_id = await _live(owner, app, paid=True)
    path = f"/api/v1/agents/{agent_id}/judge-schedule"
    assert (await owner.get(path)).json()["enabled"] is False
    saved = (await owner.put(path, json={"weekday": 0, "hour": 9, "window_days": 30})).json()
    assert saved["next_run_at"] is not None
    async with app.state.sessionmaker.begin() as session:
        schedule = await session.scalar(
            select(models.JudgeSchedule).where(models.JudgeSchedule.agent_id == agent_id)
        )
        assert schedule is not None
        schedule.next_run_at = datetime(2026, 1, 1, tzinfo=UTC)
    assert await start_due_batches(app.state.sessionmaker) >= 1
    async with app.state.sessionmaker() as session:
        batches = list(
            await session.scalars(
                select(models.JudgeBatch).where(models.JudgeBatch.agent_id == agent_id)
            )
        )
    assert [b.trigger for b in batches] == ["schedule"]
    after = (await owner.get(path)).json()
    assert after["next_run_at"] > "2026-11-13" and after["last_error"] is None


async def test_a_schedule_without_a_paid_key_records_why(
    new_owner: SignIn, app: FastAPI, judge: AnsweringJudge
) -> None:
    owner = await new_owner()
    agent_id = await _live(owner, app, paid=False)
    path = f"/api/v1/agents/{agent_id}/judge-schedule"
    await owner.put(path, json={"hour": 9})
    async with app.state.sessionmaker.begin() as session:
        schedule = await session.scalar(
            select(models.JudgeSchedule).where(models.JudgeSchedule.agent_id == agent_id)
        )
        assert schedule is not None
        schedule.next_run_at = datetime(2026, 1, 1, tzinfo=UTC)
    await start_due_batches(app.state.sessionmaker)
    assert "marked paid" in (await owner.get(path)).json()["last_error"]


async def test_a_new_version_is_judged_once_after_its_first_runs(
    new_owner: SignIn, app: FastAPI, judge: AnsweringJudge
) -> None:
    owner = await new_owner()
    agent_id = await _live(owner, app, paid=True)
    await owner.put(
        f"/api/v1/agents/{agent_id}/judge-schedule",
        json={"hour": 9, "new_version_runs": 2},
    )
    new_config = {**support_trace()["config"], "prompt": "A new prompt."}
    for name in ("n-1", "n-2", "n-3"):
        trace = support_trace(name, config=new_config)
        await owner.post("/api/v1/traces", json={"traces": [trace]})
        await score_now(app, owner.workspace_id)
    async with app.state.sessionmaker() as session:
        batches = list(
            await session.scalars(
                select(models.JudgeBatch).where(
                    models.JudgeBatch.agent_id == agent_id,
                    models.JudgeBatch.trigger == "new_version",
                )
            )
        )
    [batch] = batches
    done = (await owner.get(f"/api/v1/judge-batches/{batch.id}")).json()
    assert (done["runs"], done["judged"], done["status"]) == (2, 2, "finished")
