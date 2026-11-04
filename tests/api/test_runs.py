from fastapi import FastAPI

from tests.api.conftest import SignIn, seed_and_run
from tests.conftest import GOOD_SCRIPT, UNAPPROVED_SCRIPT, RunDemo


async def test_run_detail_and_trace_link_up(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    seeded = await seed_and_run(owner, app)
    run = (await owner.get(f"/api/v1/runs/{seeded.run_id}")).json()
    assert run["evaluation_status"] == "pass"
    assert [a["id"] for a in run["attempts"]] == [seeded.attempt_id]
    assert {s["layer"] for s in run["scores"]} == {"outcome", "policy", "trajectory", "response"}
    assert {s["status"] for s in run["scores"]} <= {"pass", "not_applicable"}
    events = (await owner.get(f"/api/v1/attempts/{seeded.attempt_id}/events")).json()
    assert [e["sequence_number"] for e in events] == list(range(1, len(events) + 1))
    assert events[-1]["event_type"] == "agent_finished"


async def test_run_detail_shows_the_task_its_hidden_truth_and_what_changed(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    seeded = await seed_and_run(owner, app)
    run = (await owner.get(f"/api/v1/runs/{seeded.run_id}")).json()
    assert run["task"]["task_key"] == "refund-damaged-headphones"
    assert "fixture" not in run["task"]["agent_input"]
    assert run["ground_truth"]["evaluator_spec"]["expected_refunds"][0]["amount_minor"] == 24999
    assert run["ground_truth"]["fixture"]
    added = {(c["collection"], c["change"]) for c in run["state_diff"]}
    assert ("refunds", "added") in added
    assert ("approvals", "added") in added


async def test_runs_filter_by_category_and_failure_reason(
    new_owner: SignIn, app: FastAPI, run_demo: RunDemo
) -> None:
    owner = await new_owner()
    demo = await run_demo(
        app.state.sessionmaker, owner.workspace_id, [GOOD_SCRIPT, UNAPPROVED_SCRIPT]
    )
    path = f"/api/v1/experiments/{demo.experiment_id}/runs"
    every = (await owner.get(path)).json()
    assert {r["task_key"] for r in every} == {"refund-damaged-headphones"}
    category = every[0]["category"]
    assert len((await owner.get(path, params={"category": category})).json()) == 2
    assert (await owner.get(path, params={"category": "nothing"})).json() == []
    [failed] = (await owner.get(path, params={"reason_code": "UNAPPROVED_REFUND"})).json()
    assert failed["agent_config_id"] == demo.config_ids[1]
