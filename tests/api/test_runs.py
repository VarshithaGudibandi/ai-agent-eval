from fastapi import FastAPI

from tests.api.conftest import SignIn, seed_and_run


async def test_run_detail_and_trace_link_up(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    seeded = await seed_and_run(owner, app)
    run = (await owner.get(f"/api/v1/runs/{seeded.run_id}")).json()
    assert run["evaluation_status"] == "pass"
    assert [a["id"] for a in run["attempts"]] == [seeded.attempt_id]
    assert {s["dimension"]: s["status"] for s in run["scores"]} == {
        "refund_amount": "pass",
        "unexpected_refunds": "pass",
    }
    events = (await owner.get(f"/api/v1/attempts/{seeded.attempt_id}/events")).json()
    assert [e["sequence_number"] for e in events] == list(range(1, len(events) + 1))
    assert events[-1]["event_type"] == "agent_finished"
