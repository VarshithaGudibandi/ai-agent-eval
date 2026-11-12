from typing import Any

from fastapi import FastAPI
from sqlalchemy import select

from app.db import models
from tests.api.conftest import Caller, SignIn
from tests.ingest.sample_traces import CONFIG, support_trace

CASES = [
    {"key": "refund-1", "input": {"message": "Broken headphones"}},
    {"key": "lookup-1", "input": {"message": "Where is my order?"}},
]


async def _start(owner: Caller, repetitions: int = 2, **extra: Any) -> dict[str, Any]:
    dataset = await owner.post("/api/v1/datasets", json={"name": "support", "cases": CASES})
    body = {
        "agent": "support-bot",
        "config": CONFIG,
        "label": "v1",
        "dataset_version_id": dataset.json()["id"],
        "repetitions": repetitions,
        **extra,
    }
    started = await owner.post("/api/v1/experiments", json=body)
    assert started.status_code == 201, started.text
    result: dict[str, Any] = started.json()
    return result


def _tagged(trace_id: str, experiment: str, case: str, repetition: Any) -> dict[str, Any]:
    return support_trace(
        trace_id, tags={"experiment": experiment, "case": case, "repetition": repetition}
    )


async def test_an_experiment_hands_out_its_cases_and_counts_runs(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    experiment = await _start(owner)
    assert [c["key"] for c in experiment["cases"]] == ["refund-1", "lookup-1"]
    assert (experiment["status"], experiment["expected_runs"], experiment["runs_received"]) == (
        "running",
        4,
        0,
    )
    traces = [
        _tagged(f"t-{case}-{rep}", experiment["id"], case, rep)
        for case in ("refund-1", "lookup-1")
        for rep in (1, 2)
    ]
    sent = await owner.post("/api/v1/traces", json={"traces": traces})
    assert len(sent.json()["accepted"]) == 4
    progress = (await owner.get(f"/api/v1/experiments/{experiment['id']}")).json()
    assert progress["runs_received"] == 4
    async with app.state.sessionmaker() as session:
        runs = list(
            await session.scalars(
                select(models.Run).where(models.Run.experiment_id == experiment["id"])
            )
        )
    assert sorted(r.repetition or 0 for r in runs) == [1, 1, 2, 2]
    assert {r.agent_version_id for r in runs} == {experiment["agent_version_id"]}
    finished = (await owner.post(f"/api/v1/experiments/{experiment['id']}/finish")).json()
    assert finished["status"] == "finished" and finished["finished_at"] is not None
    again = (await owner.post(f"/api/v1/experiments/{experiment['id']}/finish")).json()
    assert again["finished_at"] == finished["finished_at"]


async def test_a_retried_start_returns_the_same_experiment(new_owner: SignIn) -> None:
    owner = await new_owner()
    dataset = await owner.post("/api/v1/datasets", json={"name": "support", "cases": CASES})
    body = {"agent": "a", "config": {}, "dataset_version_id": dataset.json()["id"]}
    key = {"idempotency-key": "eval-123"}
    first = await owner.post("/api/v1/experiments", json=body, headers=key)
    again = await owner.post("/api/v1/experiments", json=body, headers=key)
    other = await owner.post("/api/v1/experiments", json={**body, "repetitions": 5}, headers=key)
    assert (first.status_code, again.status_code, other.status_code) == (201, 200, 409)
    assert first.json()["id"] == again.json()["id"]


async def test_traces_that_cannot_take_their_place_are_rejected(new_owner: SignIn) -> None:
    owner = await new_owner()
    experiment = await _start(owner)
    exp = experiment["id"]
    first = await owner.post("/api/v1/traces", json={"traces": [_tagged("ok", exp, "refund-1", 1)]})
    assert first.json()["accepted"] == ["ok"]
    changed = {**CONFIG, "prompt": "something else"}
    bad = [
        _tagged("taken", exp, "refund-1", 1),
        _tagged("too-many", exp, "refund-1", 3),
        _tagged("text-rep", exp, "refund-1", "1"),
        _tagged("no-case", exp, "missing", 1),
        _tagged("no-exp", "exp_unknown", "refund-1", 1),
        support_trace("half", tags={"experiment": exp}),
        {**_tagged("other-config", exp, "lookup-1", 1), "config": changed},
    ]
    response = (await owner.post("/api/v1/traces", json={"traces": bad})).json()
    assert response["accepted"] == []
    reasons = {r["trace_id"]: r["error"] for r in response["rejected"]}
    assert "already has a run" in reasons["taken"]
    assert "from 1 to 2" in reasons["too-many"] and "from 1 to 2" in reasons["text-rep"]
    assert "not in the experiment's dataset" in reasons["no-case"]
    assert "no experiment" in reasons["no-exp"]
    assert "needs experiment, case and repetition" in reasons["half"]
    assert "differs from the experiment's" in reasons["other-config"]
    # Resending the accepted trace is still a duplicate, not a clash over its slot.
    resent = await owner.post(
        "/api/v1/traces", json={"traces": [_tagged("ok", exp, "refund-1", 1)]}
    )
    assert resent.json()["duplicates"] == ["ok"]


async def test_another_workspaces_dataset_cannot_be_used(new_owner: SignIn) -> None:
    owner, stranger = await new_owner(), await new_owner()
    dataset = await owner.post("/api/v1/datasets", json={"name": "d", "cases": CASES})
    response = await stranger.post(
        "/api/v1/experiments",
        json={"agent": "a", "config": {}, "dataset_version_id": dataset.json()["id"]},
    )
    assert response.status_code == 404
