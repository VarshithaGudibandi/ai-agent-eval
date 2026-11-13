from datetime import UTC, datetime

from fastapi import FastAPI

from tests.api.conftest import Caller, SignIn
from tests.api.seeding import V2, add_dataset, run_experiment, send_live
from tests.ingest.sample_traces import CONFIG

# The live windows end on Nov 20: last week is Nov 13 to 20, the week before Nov 6 to 13.
END = "2026-11-20T00:00:00Z"
PREVIOUS_WEEK = datetime(2026, 11, 8, tzinfo=UTC)
LAST_WEEK = datetime(2026, 11, 15, tzinfo=UTC)


async def _agent_id(owner: Caller) -> str:
    [agent] = (await owner.get("/api/v1/agents")).json()
    return str(agent["id"])


async def test_one_experiment_is_shown_alone(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    dataset_id = await add_dataset(owner)
    experiment = await run_experiment(owner, app, dataset_id, CONFIG, crash={0})
    found = (await owner.get(f"/api/v1/agents/{await _agent_id(owner)}/health")).json()
    assert (found["mode"], found["baseline"], found["metrics"]) == ("single", None, [])
    assert found["current"]["experiment_id"] == experiment["id"]
    assert found["current"]["health"]["success_rate"] == 11 / 12


async def test_a_new_version_is_compared_with_its_baseline(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    dataset_id = await add_dataset(owner)
    v1 = await run_experiment(owner, app, dataset_id, CONFIG, crash=set())
    v2 = await run_experiment(owner, app, dataset_id, V2, crash=set(range(6)))
    found = (await owner.get(f"/api/v1/agents/{await _agent_id(owner)}/health")).json()
    assert found["mode"] == "versions"
    assert (found["baseline"]["experiment_id"], found["current"]["experiment_id"]) == (
        v1["id"],
        v2["id"],
    )
    success = {m["name"]: m for m in found["metrics"]}["success_rate"]
    assert (success["delta"], success["real"], success["worse"]) == (-0.5, True, True)


async def test_live_only_agents_compare_the_last_week_with_the_one_before(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    await send_live(owner, app, "prev", PREVIOUS_WEEK, passed=40, failed=0)
    agent_id = await _agent_id(owner)
    only_one = (
        await owner.get(f"/api/v1/agents/{agent_id}/health?end=2026-11-13T00:00:00Z")
    ).json()
    assert (only_one["mode"], only_one["current"]["health"]["runs"]) == ("single", 40)
    await send_live(owner, app, "last", LAST_WEEK, passed=20, failed=20)
    found = (await owner.get(f"/api/v1/agents/{agent_id}/health?end={END}")).json()
    assert found["mode"] == "live"
    assert (found["baseline"]["health"]["runs"], found["current"]["health"]["runs"]) == (40, 40)
    metrics = {m["name"]: m for m in found["metrics"]}
    assert (metrics["success_rate"]["delta"], metrics["success_rate"]["real"]) == (-0.5, True)
    # Only shares get a quick interval on live runs; the rest show a change and no verdict.
    assert metrics["duration_p50_ms"]["real"] is None
    nothing = (await owner.get(f"/api/v1/agents/{agent_id}/health?end=2027-06-01T00:00:00Z")).json()
    assert (nothing["mode"], nothing["current"]) == ("none", None)


async def test_failures_are_grouped_by_reason_for_the_newest_experiment(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    dataset_id = await add_dataset(owner)
    v1 = await run_experiment(owner, app, dataset_id, CONFIG, crash=set())
    await run_experiment(owner, app, dataset_id, V2, crash=set(range(6)))
    path = f"/api/v1/agents/{await _agent_id(owner)}/failures"
    found = (await owner.get(path)).json()
    assert (found["runs"], found["failed"]) == (24, 12)
    [group] = found["groups"]
    assert (group["scorer_key"], group["count"], len(group["example_runs"])) == (
        "run_completed",
        12,
        5,
    )
    assert "forgot the lookup" in group["reasons"][0]
    assert (await owner.get(f"{path}?experiment_id={v1['id']}")).json()["failed"] == 0


async def test_an_experiment_of_another_agent_is_not_found(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    dataset_id = await add_dataset(owner)
    experiment = await run_experiment(owner, app, dataset_id, CONFIG, crash=set())
    await owner.post(
        "/api/v1/experiments",
        json={"agent": "other-bot", "config": CONFIG, "dataset_version_id": dataset_id},
    )
    agents = {a["name"]: a["id"] for a in (await owner.get("/api/v1/agents")).json()}
    response = await owner.get(
        f"/api/v1/agents/{agents['other-bot']}/failures?experiment_id={experiment['id']}"
    )
    assert response.status_code == 404
