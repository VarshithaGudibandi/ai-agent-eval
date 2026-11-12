from fastapi import FastAPI

from tests.api.conftest import SignIn, score_now
from tests.ingest.sample_traces import support_trace

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
    assert await score_now(app, owner.workspace_id) == 2
    assert (await owner.post(rescore, json={})).json()["queued"] == 2
    assert await score_now(app, owner.workspace_id) == 2


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
