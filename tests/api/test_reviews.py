from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.db import models
from app.scoring import judge_job
from app.settings import Settings
from tests.api.conftest import Caller, SignIn, score_now
from tests.ingest.sample_traces import CONFIG, support_trace
from tests.scoring.scripted_judge import AnsweringJudge

QUESTIONS = [
    {"id": "looked_up", "text": "Did the agent look the order up before answering?"},
    {"id": "says_shipped", "text": "Does the reply say the order has shipped?"},
]
SPEC = {
    "scorers": [
        {"key": "ran", "check": "run_completed"},
        {"key": "support", "check": "checklist", "params": {"questions": QUESTIONS}},
    ]
}
QUOTES = {"looked_up": '"status": "shipped"', "says_shipped": "shipped yesterday"}


@pytest.fixture
def judge(monkeypatch: pytest.MonkeyPatch, app: FastAPI) -> AnsweringJudge:
    scripted = AnsweringJudge({qid: ("yes", quote) for qid, quote in QUOTES.items()})
    # Sampling picks a share of runs by their random ids; these tests queue runs by hand.
    app.state.settings = app.state.settings.model_copy(update={"review_sample_share": 0.0})

    @asynccontextmanager
    async def provider(api_key: str, _: Settings) -> AsyncIterator[AnsweringJudge]:
        yield scripted

    monkeypatch.setattr(judge_job, "judge_provider", provider)
    return scripted


async def _judged_run(owner: Caller, app: FastAPI) -> tuple[str, str]:
    """An experiment run graded by the checklist judge. Returns the agent and run ids."""
    await owner.post(
        "/api/v1/provider-credentials",
        json={"provider": "gemini", "label": "judge", "api_key": "AIza" + "R" * 35},
    )
    cases = [{"key": "where", "input": {"message": "Where is my order?"}}]
    dataset = (await owner.post("/api/v1/datasets", json={"name": "d", "cases": cases})).json()
    experiment = (
        await owner.post(
            "/api/v1/experiments",
            json={"agent": "support-bot", "config": CONFIG, "dataset_version_id": dataset["id"]},
        )
    ).json()
    [agent] = (await owner.get("/api/v1/agents")).json()
    await owner.put(f"/api/v1/agents/{agent['id']}/eval-spec", json=SPEC)
    tags = {"experiment": experiment["id"], "case": "where", "repetition": 1}
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-1", tags=tags)]})
    await score_now(app, owner.workspace_id)
    async with app.state.sessionmaker() as session:
        run_id = await session.scalar(
            select(models.Run.id).where(models.Run.workspace_id == owner.workspace_id)
        )
    return agent["id"], str(run_id)


async def test_a_reviewer_labels_blind_and_calibration_counts_it(
    new_owner: SignIn, app: FastAPI, judge: AnsweringJudge
) -> None:
    owner = await new_owner()
    agent_id, run_id = await _judged_run(owner, app)
    run = (await owner.get(f"/api/v1/runs/{run_id}")).json()
    assert [(a["question_id"], a["answer"], a["trusted"]) for a in run["judge_answers"]] == [
        ("looked_up", "yes", False),
        ("says_shipped", "yes", False),
    ]
    added = await owner.post("/api/v1/review-items", json={"run_id": run_id})
    again = await owner.post("/api/v1/review-items", json={"run_id": run_id})
    assert (added.status_code, again.status_code) == (201, 200)
    [item] = (await owner.get("/api/v1/review-queue")).json()
    assert item["reply"] == {"reply": "Your order ord_1001 shipped yesterday."}
    # The judge's answers are not shown to the person labelling.
    assert item["questions"] == [
        {"scorer_key": "support", "question_id": q["id"], "text": q["text"]} for q in QUESTIONS
    ]
    labels = f"/api/v1/review-items/{item['id']}/label"
    unknown = await owner.post(
        labels,
        json={"labels": [{"scorer_key": "support", "question_id": "made_up", "answer": "no"}]},
    )
    assert unknown.status_code == 422 and "made_up" in unknown.text
    labelled = await owner.post(
        labels,
        json={
            "labels": [
                {"scorer_key": "support", "question_id": "looked_up", "answer": "yes"},
                {"scorer_key": "support", "question_id": "says_shipped", "answer": "no"},
            ],
            "note": "It said shipped but the customer asked about delivery.",
        },
    )
    assert labelled.json() == {"id": item["id"], "status": "labeled", "labels": 2}
    assert (await owner.get("/api/v1/review-queue")).json() == []
    calibration = (await owner.get(f"/api/v1/agents/{agent_id}/calibration")).json()
    assert [(q["question_id"], q["labels"], q["agreement"], q["trusted"]) for q in calibration] == [
        ("looked_up", 1, 1.0, False),
        ("says_shipped", 1, 0.0, False),
    ]


async def test_saving_the_spec_again_takes_labelled_runs_as_examples(
    new_owner: SignIn, app: FastAPI, judge: AnsweringJudge
) -> None:
    owner = await new_owner()
    agent_id, run_id = await _judged_run(owner, app)
    await owner.post("/api/v1/review-items", json={"run_id": run_id})
    [item] = (await owner.get("/api/v1/review-queue")).json()
    await owner.post(
        f"/api/v1/review-items/{item['id']}/label",
        json={"labels": [{"scorer_key": "support", "question_id": "looked_up", "answer": "yes"}]},
    )
    path = f"/api/v1/agents/{agent_id}/eval-spec"
    before = (await owner.get(path)).json()
    saved = await owner.put(path, json=SPEC)
    # The same scorers now carry a worked example, so the judge is a new version.
    assert (saved.status_code, saved.json()["version"]) == (201, before["version"] + 1)
    support: dict[str, Any] = saved.json()["scorers"][1]
    [example] = support["params"]["examples"]
    assert (example["question_id"], example["answer"]) == ("looked_up", "yes")
    assert "shipped yesterday" in example["excerpt"]
    assert saved.json()["scorers"][0]["id"] == before["scorers"][0]["id"]
    # A run of a new experiment is judged with the example in its prompt.
    [dataset] = (await owner.get("/api/v1/datasets")).json()
    experiment = (
        await owner.post(
            "/api/v1/experiments",
            json={
                "agent": "support-bot",
                "config": CONFIG,
                "dataset_version_id": dataset["versions"][-1]["id"],
            },
        )
    ).json()
    tags = {"experiment": experiment["id"], "case": "where", "repetition": 1}
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-2", tags=tags)]})
    await score_now(app, owner.workspace_id)
    _, prompt = judge.prompts[-1]
    assert "Example for question looked_up, answered yes by a person" in prompt.content
    # A live run is graded by its rules alone.
    calls = judge.calls
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-live")]})
    await score_now(app, owner.workspace_id)
    assert judge.calls == calls
