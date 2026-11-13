from datetime import UTC, datetime, timedelta

from fastapi import FastAPI

from tests.api.conftest import SignIn, score_now
from tests.api.seeding import V2, add_dataset, run_experiment
from tests.ingest.sample_traces import CONFIG, support_trace


async def test_a_worse_version_is_held_with_its_flipped_cases(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    dataset_id = await add_dataset(owner)
    v1 = await run_experiment(owner, app, dataset_id, CONFIG, crash=set())
    v2 = await run_experiment(owner, app, dataset_id, V2, crash=set(range(6)))
    health = (await owner.get(f"/api/v1/experiments/{v2['id']}")).json()["health"]
    assert (health["success_rate"], health["rule_violation_rate"]) == (0.5, 0.5)
    created = await owner.post(
        "/api/v1/comparisons",
        json={"baseline": {"experiment_id": v1["id"]}, "candidate": {"experiment_id": v2["id"]}},
    )
    assert created.status_code == 201, created.text
    result = created.json()["result"]
    assert result["paired"] and result["fairness"] == []
    success = next(m for m in result["metrics"] if m["name"] == "success_rate")
    assert (success["delta"], success["real"], success["worse"]) == (-0.5, True, True)
    assert [c["outcome"] for c in result["cases"]] == ["worse"] * 6 + ["same"] * 6
    assert result["new_hard_failures"] == ["run_completed"]
    assert result["decision"] == "hold"
    stored = await owner.get(f"/api/v1/comparisons/{created.json()['id']}")
    assert stored.json()["result"]["decision"] == "hold"


async def test_gates_decide_and_are_kept_per_agent(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    dataset_id = await add_dataset(owner)
    v1 = await run_experiment(owner, app, dataset_id, CONFIG, crash={0})
    v2 = await run_experiment(owner, app, dataset_id, V2, crash={0, 1})
    [agent] = (await owner.get("/api/v1/agents")).json()
    path = f"/api/v1/agents/{agent['id']}/gates"
    assert (await owner.get(path)).json()["max_success_drop"] == 0.05
    lenient = {
        "max_success_drop": 0.5,
        "max_cost_increase": None,
        "hold_on_new_hard_failure": False,
    }
    assert (await owner.put(path, json=lenient)).json() == lenient
    body = {"baseline": {"experiment_id": v1["id"]}, "candidate": {"experiment_id": v2["id"]}}
    result = (await owner.post("/api/v1/comparisons", json=body)).json()
    assert result["gates"] == lenient and result["result"]["decision"] == "ship"
    # A one-off override does not change what is stored.
    strict = {**body, "gates": {"max_success_drop": 0.0, "hold_on_new_hard_failure": True}}
    held = (await owner.post("/api/v1/comparisons", json=strict)).json()
    assert held["gates"]["max_success_drop"] == 0.0 and held["gates"]["max_cost_increase"] == 0.2
    assert (await owner.get(path)).json() == lenient


async def test_the_baseline_is_the_shipped_version_and_then_any_other(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    dataset_id = await add_dataset(owner)
    v1 = await run_experiment(owner, app, dataset_id, CONFIG, crash=set())
    v2 = await run_experiment(owner, app, dataset_id, V2, crash={0})
    v3_config = {**CONFIG, "prompt": "Third try."}
    v3 = await run_experiment(owner, app, dataset_id, v3_config, crash=set())
    baseline = f"/api/v1/experiments/{v3['id']}/baseline"
    # Nothing shipped yet: the newest other version.
    assert (await owner.get(baseline)).json()["experiment_id"] == v2["id"]
    [agent] = (await owner.get("/api/v1/agents")).json()
    shipped = await owner.post(
        f"/api/v1/agents/{agent['id']}/shipped", json={"version_id": v1["agent_version_id"]}
    )
    assert shipped.json()["shipped_version_id"] == v1["agent_version_id"]
    assert (await owner.get(baseline)).json()["experiment_id"] == v1["id"]
    first = f"/api/v1/experiments/{v1['id']}/baseline"
    assert (await owner.get(first)).json()["experiment_id"] in (v2["id"], v3["id"])


async def test_live_windows_compare_unpaired_and_mixed_sides_are_refused(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    await owner.post("/api/v1/traces", json={"traces": [support_trace("live-1")]})
    await score_now(app, owner.workspace_id)
    [agent] = (await owner.get("/api/v1/agents")).json()
    start = datetime(2026, 11, 1, tzinfo=UTC)
    week = {"agent_id": agent["id"], "start": start.isoformat()}
    body = {
        "baseline": {**week, "end": (start + timedelta(days=11)).isoformat()},
        "candidate": {**week, "end": (start + timedelta(days=30)).isoformat()},
    }
    result = (await owner.post("/api/v1/comparisons", json=body)).json()["result"]
    assert result["paired"] is False and result["cases"] == []
    assert result["candidate"]["runs"] == 1 and result["decision"] == "insufficient_data"
    mixed = {"baseline": body["baseline"], "candidate": {"experiment_id": "exp_x"}}
    assert (await owner.post("/api/v1/comparisons", json=mixed)).status_code == 422
