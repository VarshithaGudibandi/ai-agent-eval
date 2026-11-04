"""Runs only against a live Compose stack: STACK_URL=http://127.0.0.1:8000 pytest tests/stack.

It proves the external-agent path end to end with real containers: the example agent runs
in its own container, the worker calls it over the Compose network, and it calls the
gateway back over HTTP.
"""

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

STACK_URL = os.environ.get("STACK_URL")
ROOT = Path(__file__).parents[2]
TASKS = ["refund_damaged_headphones", "lookup_order_shipped", "ineligible_return_in_transit"]

pytestmark = pytest.mark.skipif(not STACK_URL, reason="needs a running stack (STACK_URL)")


def _until(check: Callable[[], bool], seconds: float = 90) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(1)
    raise AssertionError("timed out")


def test_the_example_agent_runs_in_its_own_container_and_is_graded() -> None:
    api = httpx.Client(base_url=str(STACK_URL), timeout=30)
    email = f"stack-{int(time.time())}@example.com"
    password = "stack-check-password"
    setup = api.post(
        "/api/v1/auth/setup",
        json={"email": email, "password": password, "workspace_name": "Stack"},
    )
    if setup.status_code != 201:
        pytest.skip("the stack already has an owner; run this against a fresh stack")
    login = api.post("/api/v1/auth/login", json={"email": email, "password": password}).json()
    headers = {"x-csrf-token": login["csrf_token"]}
    secret = api.post("/api/v1/agent-signing-secret", headers=headers).json()["secret"]

    docker = shutil.which("docker")
    assert docker is not None
    # Fixed arguments only; the secret travels in the environment, never the command line.
    subprocess.run(  # noqa: S603
        [docker, "compose", "--profile", "example", "up", "-d", "--build", "example-agent"],
        cwd=ROOT,
        env={**os.environ, "AGENT_SIGNING_SECRET": secret},
        check=True,
    )
    config = api.post(
        "/api/v1/agent-configs",
        json={
            "name": "example",
            "adapter_type": "http",
            "settings": {"url": "http://example-agent:8080/"},
        },
        headers=headers,
    ).json()

    def handshake_ok() -> bool:
        response = api.post(f"/api/v1/agent-configs/{config['id']}/check", headers=headers)
        assert response.status_code == 200, response.text
        return bool(response.json()["ok"])

    # The API checks the agent with a signed handshake, container to container.
    _until(handshake_ok)

    tasks = []
    for name in TASKS:
        task = json.loads((ROOT / f"datasets/support_retail/tasks/{name}.json").read_text())
        tasks.append({**task, "split": "dev"})
    dataset = api.post(
        "/api/v1/datasets/import", json={"name": "stack", "tasks": tasks}, headers=headers
    ).json()
    experiment = api.post(
        "/api/v1/experiments",
        json={
            "dataset_version_id": dataset["dataset_version_id"],
            "agent_config_ids": [config["id"]],
            "mode": "real",
        },
        headers=headers,
    ).json()
    _until(
        lambda: api.get(f"/api/v1/experiments/{experiment['id']}").json()["status"] == "completed"
    )
    runs = api.get(f"/api/v1/experiments/{experiment['id']}/runs").json()
    assert [(r["execution_status"], r["evaluation_status"]) for r in runs] == [
        ("completed", "pass")
    ] * 3
