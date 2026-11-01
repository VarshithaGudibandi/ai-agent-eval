from fastapi import FastAPI

from tests.api.conftest import SCRIPT, TASK, Caller, SignIn, seed_and_run


async def _ids(owner: Caller) -> tuple[str, str]:
    version = await owner.post("/api/v1/datasets/import", json={"name": "r", "tasks": [TASK]})
    config = await owner.post(
        "/api/v1/agent-configs", json={"name": "s", "adapter_type": "scripted", "settings": SCRIPT}
    )
    return version.json()["dataset_version_id"], config.json()["id"]


async def test_idempotency_key_returns_the_first_experiment(new_owner: SignIn) -> None:
    owner = await new_owner()
    version, config = await _ids(owner)
    body = {"dataset_version_id": version, "agent_config_ids": [config], "mode": "demo"}
    key = {"idempotency-key": "launch-1"}
    first = await owner.post("/api/v1/experiments", json=body, headers=key)
    again = await owner.post("/api/v1/experiments", json=body, headers=key)
    changed = await owner.post("/api/v1/experiments", json={**body, "repetitions": 3}, headers=key)
    assert (first.status_code, again.status_code, changed.status_code) == (201, 200, 409)
    assert first.json()["id"] == again.json()["id"]
    assert len((await owner.get("/api/v1/experiments")).json()) == 1


async def test_scripted_configs_are_refused_in_real_mode(new_owner: SignIn) -> None:
    owner = await new_owner()
    version, config = await _ids(owner)
    response = await owner.post(
        "/api/v1/experiments",
        json={"dataset_version_id": version, "agent_config_ids": [config], "mode": "real"},
    )
    assert (response.status_code, response.json()["error"]) == (422, "experiment_rejected")


async def test_finished_experiment_shows_progress_and_filters_runs(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    seeded = await seed_and_run(owner, app)
    detail = (await owner.get(f"/api/v1/experiments/{seeded.experiment_id}")).json()
    assert (detail["status"], detail["runs_by_evaluation_status"]) == ("completed", {"pass": 1})
    failing = await owner.get(
        f"/api/v1/experiments/{seeded.experiment_id}/runs", params={"evaluation_status": "fail"}
    )
    assert failing.json() == []


async def test_budget_limits_are_stored_and_reported(new_owner: SignIn) -> None:
    owner = await new_owner()
    version, config = await _ids(owner)
    created = await owner.post(
        "/api/v1/experiments",
        json={
            "dataset_version_id": version,
            "agent_config_ids": [config],
            "mode": "demo",
            "budget": {"money_micros": 2_000_000, "tokens": 50_000, "requests": 100},
        },
    )
    budget = created.json()["budget"]
    assert (budget["money_limit_micros"], budget["tokens_limit"], budget["requests_limit"]) == (
        2_000_000,
        50_000,
        100,
    )
    assert (budget["money_used_micros"], budget["warning"]) == (0, False)
