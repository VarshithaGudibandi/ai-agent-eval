from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.db import models
from app.scoring import judge_job
from app.settings import Settings
from tests.api.conftest import Caller, score_now
from tests.ingest.sample_traces import CONFIG, support_trace
from tests.scoring.scripted_judge import AnsweringJudge

# Seeding through the API, so tests see data exactly as the platform stores it.

CASES = [{"key": f"case-{n}", "input": {"n": n}} for n in range(12)]
V2 = {**CONFIG, "prompt": "A shorter prompt that forgets to look things up."}


async def add_dataset(owner: Caller, cases: list[dict[str, Any]] = CASES) -> str:
    """A dataset of the given cases. Returns its version id."""
    response = await owner.post("/api/v1/datasets", json={"name": "support", "cases": cases})
    return str(response.json()["id"])


async def run_experiment(
    owner: Caller,
    app: FastAPI,
    dataset_id: str,
    config: dict[str, Any],
    crash: set[int],
    cases: list[dict[str, Any]] = CASES,
) -> dict[str, Any]:
    """A finished, scored experiment, each case run twice; the listed cases crash every time."""
    experiment = (
        await owner.post(
            "/api/v1/experiments",
            json={
                "agent": "support-bot",
                "config": config,
                "dataset_version_id": dataset_id,
                "repetitions": 2,
            },
        )
    ).json()
    traces = []
    for n, case in enumerate(cases):
        for rep in (1, 2):
            trace = support_trace(
                f"{experiment['id']}-{n}-{rep}",
                config=config,
                tags={"experiment": experiment["id"], "case": case["key"], "repetition": rep},
            )
            if n in crash:
                trace["spans"][0]["error"] = "RuntimeError: forgot the lookup"
            traces.append(trace)
    await owner.post("/api/v1/traces", json={"traces": traces})
    await owner.post(f"/api/v1/experiments/{experiment['id']}/finish")
    await score_now(app, owner.workspace_id)
    return experiment


# The sample trace starts at 2026-11-12T10:00:00Z; live helpers move it to the time asked.
SAMPLE_START = datetime.fromisoformat("2026-11-12T10:00:00+00:00")


def at(trace: dict[str, Any], when: datetime) -> dict[str, Any]:
    """The trace with every span moved so the run starts at when."""
    shift = when - SAMPLE_START
    for span in trace["spans"]:
        for field in ("start", "end"):
            moved = datetime.fromisoformat(span[field]) + shift
            span[field] = moved.isoformat()
    return trace


async def send_live(
    owner: Caller, app: FastAPI, prefix: str, day: datetime, passed: int, failed: int
) -> None:
    """Live runs spread over one day, the last `failed` of them crashing, all scored."""
    traces = []
    for n in range(passed + failed):
        trace = at(support_trace(f"{prefix}-{n}"), day + timedelta(minutes=n))
        if n >= passed:
            trace["spans"][0]["error"] = "RuntimeError: timed out"
        traces.append(trace)
    for start in range(0, len(traces), 100):
        await owner.post("/api/v1/traces", json={"traces": traces[start : start + 100]})
    await score_now(app, owner.workspace_id)


QUESTION = {"id": "says_shipped", "text": "Does the reply say the order has shipped?"}


async def judged_live_run(owner: Caller, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> str:
    """A live run judged by a scripted checklist judge through a judge batch. Returns its id.
    Nothing is sampled into the review queue."""
    scripted = AnsweringJudge({"says_shipped": ("yes", "shipped yesterday")})

    @asynccontextmanager
    async def provider(api_key: str, _: Settings) -> AsyncIterator[AnsweringJudge]:
        yield scripted

    monkeypatch.setattr(judge_job, "judge_provider", provider)
    app.state.settings = app.state.settings.model_copy(update={"review_sample_share": 0.0})
    await owner.post(
        "/api/v1/provider-credentials",
        json={"provider": "gemini", "label": "j", "api_key": "AIza" + "B" * 35, "paid": True},
    )
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-0")]})
    [agent] = (await owner.get("/api/v1/agents")).json()
    judge = {"key": "support", "check": "checklist", "params": {"questions": [QUESTION]}}
    await owner.put(f"/api/v1/agents/{agent['id']}/eval-spec", json={"scorers": [judge]})
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    await score_now(app, owner.workspace_id)
    window = {"start": "2026-11-01T00:00:00Z", "end": "2026-12-01T00:00:00Z"}
    await owner.post("/api/v1/judge-batches", json={"agent_id": agent["id"], **window})
    await score_now(app, owner.workspace_id)
    async with app.state.sessionmaker() as session:
        run_id = await session.scalar(
            select(models.Run.id)
            .join(models.Trace, models.Trace.id == models.Run.trace_row_id)
            .where(models.Trace.workspace_id == owner.workspace_id, models.Trace.trace_id == "t-1")
        )
    return str(run_id)
