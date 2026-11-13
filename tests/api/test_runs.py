from fastapi import FastAPI
from sqlalchemy import select

from app.db import models
from tests.api.conftest import SignIn, score_now
from tests.ingest.sample_traces import CONFIG, support_trace


async def _run_id(app: FastAPI, workspace_id: str, trace_id: str) -> str:
    async with app.state.sessionmaker() as session:
        run_id = await session.scalar(
            select(models.Run.id)
            .join(models.Trace, models.Trace.id == models.Run.trace_row_id)
            .where(models.Trace.workspace_id == workspace_id, models.Trace.trace_id == trace_id)
        )
    assert run_id is not None
    return str(run_id)


async def test_a_run_shows_its_spans_scores_and_verdict(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    run_id = await _run_id(app, owner.workspace_id, "t-1")
    before = (await owner.get(f"/api/v1/runs/{run_id}")).json()
    assert (before["trace_id"], before["scores"], before["verdicts"]) == ("t-1", [], [])
    await score_now(app, owner.workspace_id)
    run = (await owner.get(f"/api/v1/runs/{run_id}")).json()
    assert {s["kind"] for s in run["spans"]} == {"step", "model", "tool"}
    [verdict] = run["verdicts"]
    assert (verdict["eval_spec_version"], verdict["status"]) == (1, "passed")
    assert {s["scorer_key"] for s in run["scores"]} >= {"run_completed", "turn_limit"}
    assert all(s["passed"] for s in run["scores"])


async def test_experiment_progress_counts_scored_and_failed_runs(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    cases = [{"key": "a", "input": 1}, {"key": "b", "input": 2}]
    dataset = (await owner.post("/api/v1/datasets", json={"name": "d", "cases": cases})).json()
    experiment = (
        await owner.post(
            "/api/v1/experiments",
            json={"agent": "support-bot", "config": CONFIG, "dataset_version_id": dataset["id"]},
        )
    ).json()
    tagged = [
        support_trace(
            f"t-{key}", tags={"experiment": experiment["id"], "case": key, "repetition": 1}
        )
        for key in ("a", "b")
    ]
    tagged[1]["spans"][0]["error"] = "RuntimeError: crashed"
    await owner.post("/api/v1/traces", json={"traces": tagged})
    path = f"/api/v1/experiments/{experiment['id']}"
    assert (await owner.get(path)).json()["runs_scored"] == 0
    await score_now(app, owner.workspace_id)
    progress = (await owner.get(path)).json()
    assert (progress["runs_scored"], progress["runs_passed"], progress["failures"]) == (
        2,
        1,
        {"run_completed": 1},
    )
    assert progress["errors"] == {}


async def test_an_experiment_run_shows_its_case_and_the_other_repetitions(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    cases = [
        {"key": "a", "input": {"q": "where is my order?"}, "expected": "look it up"},
        {"key": "b", "input": {"q": "other"}},
    ]
    dataset = (await owner.post("/api/v1/datasets", json={"name": "d", "cases": cases})).json()
    experiment = (
        await owner.post(
            "/api/v1/experiments",
            json={
                "agent": "support-bot",
                "config": CONFIG,
                "dataset_version_id": dataset["id"],
                "repetitions": 3,
            },
        )
    ).json()
    tagged = [
        support_trace(f"t-{n}", tags={"experiment": experiment["id"], "case": "a", "repetition": n})
        for n in (1, 2, 3)
    ]
    tagged[1]["spans"][0]["error"] = "RuntimeError: crashed"
    other = support_trace(
        "t-b", tags={"experiment": experiment["id"], "case": "b", "repetition": 1}
    )
    await owner.post("/api/v1/traces", json={"traces": [*tagged[:2], other]})
    await score_now(app, owner.workspace_id)
    await owner.post("/api/v1/traces", json={"traces": tagged[2:]})
    run = (await owner.get(f"/api/v1/runs/{await _run_id(app, owner.workspace_id, 't-2')}")).json()
    assert run["case"] == {
        "key": "a",
        "input": {"q": "where is my order?"},
        "expected": "look it up",
    }
    assert [(r["repetition"], r["status"]) for r in run["repetitions"]] == [
        (1, "passed"),
        (2, "failed"),
        (3, None),
    ]
    assert run["id"] in [r["run_id"] for r in run["repetitions"]]


async def test_a_live_run_has_no_case_or_repetitions(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    run = (await owner.get(f"/api/v1/runs/{await _run_id(app, owner.workspace_id, 't-1')}")).json()
    assert (run["case"], run["repetitions"]) == (None, [])
