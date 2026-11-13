from typing import Any

from fastapi import FastAPI

from tests.api.conftest import Caller, score_now
from tests.ingest.sample_traces import CONFIG, support_trace

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
