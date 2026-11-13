from fastapi import FastAPI

from tests.api.conftest import SignIn, score_now
from tests.api.seeding import V2, add_dataset, run_experiment
from tests.ingest.sample_traces import CONFIG, support_trace

STRICT = {
    "scorers": [
        {"key": "ran", "check": "run_completed"},
        {
            "key": "lookup_first",
            "check": "tool_order",
            "params": {"tool": "issue_refund", "after": ["get_order"]},
        },
        {
            "key": "thanks",
            "check": "reply_contains",
            "params": {"text": "thank"},
            "severity": "soft",
        },
    ]
}


async def test_a_new_agent_gets_the_built_in_spec_and_can_save_its_own(
    new_owner: SignIn,
) -> None:
    owner = await new_owner()
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    [agent] = (await owner.get("/api/v1/agents")).json()
    assert (agent["name"], len(agent["versions"]), agent["eval_spec_version"]) == (
        "support-bot",
        1,
        1,
    )
    path = f"/api/v1/agents/{agent['id']}/eval-spec"
    default = (await owner.get(path)).json()
    assert [s["key"] for s in default["scorers"]] == [
        "run_completed",
        "tool_errors",
        "valid_tool_arguments",
        "turn_limit",
    ]
    saved = await owner.put(path, json=STRICT)
    again = await owner.put(path, json=STRICT)
    assert (saved.status_code, again.status_code) == (201, 200)
    assert saved.json()["version"] == again.json()["version"] == 2
    thanks = saved.json()["scorers"][2]
    assert (thanks["kind"], thanks["severity"], thanks["threshold"]) == ("rule", "soft", 1.0)
    assert (await owner.get(f"{path}?version=1")).json()["id"] == default["id"]
    assert (await owner.get(f"{path}?version=9")).status_code == 404


async def test_a_bad_spec_is_refused_with_the_reason(new_owner: SignIn) -> None:
    owner = await new_owner()
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    [agent] = (await owner.get("/api/v1/agents")).json()
    response = await owner.put(
        f"/api/v1/agents/{agent['id']}/eval-spec",
        json={"scorers": [{"key": "x", "check": "vibes"}]},
    )
    assert response.status_code == 422
    assert "unknown check 'vibes'" in response.text


async def test_rescoring_grades_old_runs_under_the_new_spec(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    await owner.post(
        "/api/v1/traces", json={"traces": [support_trace("t-1"), support_trace("t-2")]}
    )
    [agent] = (await owner.get("/api/v1/agents")).json()
    await owner.put(f"/api/v1/agents/{agent['id']}/eval-spec", json=STRICT)
    rescore = f"/api/v1/agents/{agent['id']}/rescore"
    # Both runs are still waiting for their first scoring, so nothing new is queued.
    assert (await owner.post(rescore, json={})).json() == {
        "eval_spec_version": 2,
        "queued": 0,
        "already_waiting": 2,
    }
    # Two scoring jobs, then one job to rebuild the day the two live runs fall on.
    assert await score_now(app, owner.workspace_id) == 3
    assert (await owner.post(rescore, json={})).json()["queued"] == 2
    assert await score_now(app, owner.workspace_id) == 3


async def test_a_judge_from_a_pack_can_be_saved_and_read(new_owner: SignIn) -> None:
    owner = await new_owner()
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    [agent] = (await owner.get("/api/v1/agents")).json()
    path = f"/api/v1/agents/{agent['id']}/eval-spec"
    spec = {"scorers": [{"key": "support", "check": "checklist", "params": {"pack": "support"}}]}
    saved = await owner.put(path, json=spec)
    again = await owner.put(path, json=spec)
    assert (saved.status_code, again.status_code) == (201, 200)
    [judge] = (await owner.get(path)).json()["scorers"]
    assert (judge["kind"], judge["severity"], judge["threshold"]) == ("judge", "soft", 0.7)
    assert len(judge["params"]["questions"]) == 3


async def test_live_health_is_kept_per_version_and_day(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    crashed = support_trace("t-2")
    crashed["spans"][0]["error"] = "RuntimeError: down"
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-1"), crashed]})
    await score_now(app, owner.workspace_id)
    [agent] = (await owner.get("/api/v1/agents")).json()
    path = f"/api/v1/agents/{agent['id']}/health/daily?days=365"
    [day] = (await owner.get(path)).json()
    assert (day["day"], day["runs"], day["health"]["success_rate"]) == ("2026-11-12", 2, 0.5)
    # A later run on the same day rebuilds that day, not a second row.
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-3")]})
    await score_now(app, owner.workspace_id)
    [day] = (await owner.get(path)).json()
    assert (day["runs"], day["health"]["success_rate"]) == (3, 2 / 3)


async def test_an_agents_experiments_come_newest_first_with_their_health(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    dataset_id = await add_dataset(owner)
    first = await run_experiment(owner, app, dataset_id, CONFIG, crash=set())
    second = await run_experiment(owner, app, dataset_id, V2, crash={0, 1, 2})
    [agent] = (await owner.get("/api/v1/agents")).json()
    listed = (await owner.get(f"/api/v1/agents/{agent['id']}/experiments")).json()
    assert [e["id"] for e in listed] == [second["id"], first["id"]]
    assert [e["health"]["success_rate"] for e in listed] == [0.75, 1.0]
    assert listed[0]["expected_runs"] == listed[0]["runs_received"] == 24
    assert len((await owner.get(f"/api/v1/agents/{agent['id']}/experiments?limit=1")).json()) == 1
