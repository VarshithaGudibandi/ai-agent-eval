from collections.abc import Callable

import httpx
from fastapi import FastAPI

from tests.api.conftest import SCRIPT, TASK, SignIn, token_caller
from tests.conftest import GOOD_SCRIPT, UNAPPROVED_SCRIPT, RunDemo


async def test_a_regression_shows_up_task_by_task(
    new_owner: SignIn, app: FastAPI, run_demo: RunDemo
) -> None:
    owner = await new_owner()
    demo = await run_demo(
        app.state.sessionmaker, owner.workspace_id, [GOOD_SCRIPT, UNAPPROVED_SCRIPT], 2
    )
    v1, v2 = demo.config_ids
    created = await owner.post(
        "/api/v1/comparisons",
        json={
            "baseline": {"experiment_id": demo.experiment_id, "agent_config_id": v1},
            "candidate": {"experiment_id": demo.experiment_id, "agent_config_id": v2},
        },
    )
    assert created.status_code == 201, created.text
    result = created.json()["result"]
    assert (result["losses"], result["wins"], result["ties"]) == (1, 0, 0)
    [task] = result["tasks"]
    assert task["task_key"] == "refund-damaged-headphones"
    assert (task["outcome"], task["baseline"]["passed"], task["candidate"]["passed"]) == (
        "loss",
        2,
        0,
    )
    assert len(task["candidate"]["run_ids"]) == 2
    assert task["candidate"]["reason_codes"]
    assert result["may_be_noise"] is True
    again = await owner.get(f"/api/v1/comparisons/{created.json()['id']}")
    assert again.json() == created.json()
    [listed] = (await owner.get("/api/v1/comparisons")).json()
    assert (listed["id"], listed["losses"]) == (created.json()["id"], 1)


async def test_comparisons_name_the_field_that_differs(
    new_owner: SignIn, app: FastAPI, run_demo: RunDemo
) -> None:
    owner = await new_owner()
    first = await run_demo(app.state.sessionmaker, owner.workspace_id, [GOOD_SCRIPT])
    # A second import is a new dataset version, so the tasks are not the same.
    second = await run_demo(app.state.sessionmaker, owner.workspace_id, [UNAPPROVED_SCRIPT])
    response = await owner.post(
        "/api/v1/comparisons",
        json={
            "baseline": {"experiment_id": first.experiment_id},
            "candidate": {"experiment_id": second.experiment_id},
        },
    )
    assert response.status_code == 422
    assert (response.json()["error"], response.json()["field"]) == (
        "incompatible_experiments",
        "dataset_version_id",
    )


async def test_a_side_with_several_configs_must_name_one(
    new_owner: SignIn, app: FastAPI, run_demo: RunDemo
) -> None:
    owner = await new_owner()
    demo = await run_demo(
        app.state.sessionmaker, owner.workspace_id, [GOOD_SCRIPT, UNAPPROVED_SCRIPT]
    )
    side = {"experiment_id": demo.experiment_id}
    response = await owner.post("/api/v1/comparisons", json={"baseline": side, "candidate": side})
    assert (response.status_code, response.json()["error"]) == (422, "comparison_side_unclear")


async def test_an_unfinished_experiment_is_not_compared(new_owner: SignIn) -> None:
    owner = await new_owner()
    version = await owner.post("/api/v1/datasets/import", json={"name": "r", "tasks": [TASK]})
    configs = [
        (
            await owner.post(
                "/api/v1/agent-configs",
                json={"name": name, "adapter_type": "scripted", "settings": SCRIPT},
            )
        ).json()["id"]
        for name in ("v1", "v2")
    ]
    queued = await owner.post(
        "/api/v1/experiments",
        json={
            "dataset_version_id": version.json()["dataset_version_id"],
            "agent_config_ids": configs,
            "mode": "demo",
        },
    )
    sides = [{"experiment_id": queued.json()["id"], "agent_config_id": c} for c in configs]
    response = await owner.post(
        "/api/v1/comparisons", json={"baseline": sides[0], "candidate": sides[1]}
    )
    assert (response.status_code, response.json()["error"]) == (409, "experiment_not_finished")


async def test_viewers_read_comparisons_but_cannot_make_them(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    viewer = await token_caller(owner, clients(), role="viewer")
    side = {"experiment_id": "exp_missing"}
    made = await viewer.post("/api/v1/comparisons", json={"baseline": side, "candidate": side})
    assert made.status_code == 403
    assert (await viewer.get("/api/v1/comparisons")).status_code == 200
