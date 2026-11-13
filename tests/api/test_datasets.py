from collections.abc import Callable

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.db import models
from tests.api.conftest import SignIn, token_caller
from tests.api.seeding import add_dataset, run_experiment
from tests.ingest.sample_traces import CONFIG, LEAKED_KEY, support_trace

CASES = [
    {"key": "refund-1", "input": {"message": "Broken headphones"}, "expected": {"refund": 249.99}},
    {"key": "lookup-1", "input": {"message": "Where is my order?"}, "tags": {"category": "lookup"}},
]


async def test_cases_are_stored_as_versions_and_read_back(new_owner: SignIn) -> None:
    owner = await new_owner()
    first = await owner.post("/api/v1/datasets", json={"name": "support", "cases": CASES})
    assert first.status_code == 201
    again = await owner.post("/api/v1/datasets", json={"name": "support", "cases": CASES})
    assert (first.json()["created"], again.json()["created"]) == (True, False)
    assert again.json()["id"] == first.json()["id"]
    added = await owner.post(
        f"/api/v1/datasets/{first.json()['dataset_id']}/cases",
        json={"cases": [{"key": "new-1", "input": "hello"}]},
    )
    assert (added.json()["version"], added.json()["case_count"]) == (2, 3)
    version = (await owner.get(f"/api/v1/dataset-versions/{added.json()['id']}")).json()
    assert [c["key"] for c in version["cases"]] == ["refund-1", "lookup-1", "new-1"]
    assert version["cases"][0]["expected"] == {"refund": 249.99}
    [dataset] = (await owner.get("/api/v1/datasets")).json()
    assert [(v["version"], v["case_count"]) for v in dataset["versions"]] == [(1, 2), (2, 3)]


async def test_case_inputs_are_masked_and_keys_checked(new_owner: SignIn) -> None:
    owner = await new_owner()
    leaky = {"key": "leak", "input": {"note": f"key is {LEAKED_KEY}"}}
    saved = await owner.post("/api/v1/datasets", json={"name": "d", "cases": [leaky]})
    version = (await owner.get(f"/api/v1/dataset-versions/{saved.json()['id']}")).json()
    assert LEAKED_KEY not in str(version)
    clash = await owner.post("/api/v1/datasets", json={"name": "d", "cases": [CASES[0], CASES[0]]})
    assert (clash.status_code, clash.json()["error"]) == (422, "case_keys_clash")


async def test_viewers_read_datasets_but_cannot_write_them(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    viewer = await token_caller(owner, clients(), role="viewer")
    refused = await viewer.post("/api/v1/datasets", json={"name": "d", "cases": CASES})
    assert refused.status_code == 403
    assert (await viewer.get("/api/v1/datasets")).status_code == 200


async def test_runs_become_cases_once_and_cases_show_their_last_result(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    dataset_id = await add_dataset(owner, CASES)
    first = (await owner.get("/api/v1/datasets")).json()[0]
    await run_experiment(owner, app, dataset_id, CONFIG, crash={1}, cases=CASES)
    version = (await owner.get(f"/api/v1/dataset-versions/{dataset_id}")).json()
    assert [c["last_result"]["status"] for c in version["cases"]] == ["passed", "failed"]
    await owner.post("/api/v1/traces", json={"traces": [support_trace("live-1")]})
    async with app.state.sessionmaker() as session:
        run_id = await session.scalar(
            select(models.Run.id)
            .join(models.Trace, models.Trace.id == models.Run.trace_row_id)
            .where(
                models.Trace.workspace_id == owner.workspace_id, models.Trace.trace_id == "live-1"
            )
        )
    path = f"/api/v1/datasets/{first['id']}/cases"
    added = (await owner.post(path, json={"run_ids": [run_id, run_id]})).json()
    assert (added["created"], added["case_count"]) == (True, 3)
    again = (await owner.post(path, json={"run_ids": [run_id]})).json()
    assert (again["created"], again["id"]) == (False, added["id"])
    case = (await owner.get(f"/api/v1/dataset-versions/{added['id']}")).json()["cases"][-1]
    assert (case["key"], case["expected"], case["last_result"]) == ("run-live-1", None, None)
    assert case["input"] == support_trace()["spans"][0]["input"]
    missing = await owner.post(path, json={"run_ids": ["run_nope"]})
    assert missing.status_code == 404
    both = await owner.post(path, json={"run_ids": [run_id], "cases": CASES})
    assert both.status_code == 422
