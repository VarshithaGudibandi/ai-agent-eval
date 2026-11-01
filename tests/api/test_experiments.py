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


async def _demo(owner: Caller, budget: dict[str, int] | None = None) -> str:
    version, config = await _ids(owner)
    created = await owner.post(
        "/api/v1/experiments",
        json={
            "dataset_version_id": version,
            "agent_config_ids": [config],
            "mode": "demo",
            "budget": budget or {},
        },
    )
    return str(created.json()["id"])


async def test_pause_resume_and_close_follow_the_allowed_order(new_owner: SignIn) -> None:
    owner = await new_owner()
    experiment_id = await _demo(owner)
    base = f"/api/v1/experiments/{experiment_id}"
    assert (await owner.post(f"{base}/resume")).status_code == 409
    paused = await owner.post(f"{base}/pause")
    assert paused.json()["status"] == "paused_user"
    assert (await owner.post(f"{base}/resume")).json()["status"] == "queued"
    await owner.post(f"{base}/pause")
    closed = await owner.post(f"{base}/close")
    assert closed.json()["status"] == "closed_partial"
    again = await owner.post(f"{base}/pause")
    assert (again.status_code, again.json()["error"]) == (409, "invalid_transition")


async def test_budget_can_rise_but_resume_needs_headroom(new_owner: SignIn) -> None:
    owner = await new_owner()
    experiment_id = await _demo(owner, {"requests": 0})
    base = f"/api/v1/experiments/{experiment_id}"
    await owner.post(f"{base}/pause")
    refused = await owner.post(f"{base}/resume")
    assert (refused.status_code, refused.json()["detail"]["error"]) == (409, "budget_exhausted")
    raised = await owner.client.patch(
        f"{base}/budget", json={"requests": 10}, headers=owner.headers
    )
    assert raised.json()["budget"]["requests_limit"] == 10
    assert (await owner.post(f"{base}/resume")).status_code == 200


async def test_tool_loop_rules_at_config_and_launch(new_owner: SignIn) -> None:
    owner = await new_owner()
    unknown = await owner.post(
        "/api/v1/agent-configs",
        json={
            "name": "x",
            "adapter_type": "tool_loop",
            "settings": {"provider": "nope", "model": "m"},
        },
    )
    assert (unknown.status_code, unknown.json()["error"]) == (422, "provider_unknown")
    config = await owner.post(
        "/api/v1/agent-configs",
        json={
            "name": "gemini",
            "adapter_type": "tool_loop",
            "settings": {"provider": "gemini", "model": "gemini-flash-lite-latest"},
        },
    )
    assert config.status_code == 201
    version, _ = await _ids(owner)
    body = {"dataset_version_id": version, "agent_config_ids": [config.json()["id"]]}
    no_key = await owner.post("/api/v1/experiments", json={**body, "mode": "real"})
    assert (no_key.status_code, no_key.json()["error"]) == (409, "provider_key_required")
    demo = await owner.post("/api/v1/experiments", json={**body, "mode": "demo"})
    assert (demo.status_code, demo.json()["error"]) == (422, "experiment_rejected")
    await owner.post(
        "/api/v1/provider-credentials",
        json={"provider": "gemini", "label": "k", "api_key": "AIzaFAKE-for-tests-0001"},
    )
    capped = await owner.post(
        "/api/v1/experiments", json={**body, "mode": "real", "budget": {"money_micros": 10}}
    )
    assert (capped.status_code, capped.json()["error"]) == (422, "price_unknown")
    uncapped = await owner.post(
        "/api/v1/experiments", json={**body, "mode": "real", "budget": {"requests": 20}}
    )
    assert uncapped.status_code == 201
