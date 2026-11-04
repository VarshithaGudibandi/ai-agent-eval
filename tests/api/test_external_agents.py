import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from fastapi import FastAPI

from app.agents.signing import SIGNATURE_HEADER, verify
from tests.api.conftest import TASK, Caller, SignIn, token_caller


@pytest.fixture
def handshake_server() -> Iterator[tuple[str, dict[str, str], list[bool]]]:
    """A tiny agent on localhost that answers handshakes and notes whether they verified."""
    verified: list[bool] = []
    secret_box: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["content-length"]))
            secret = secret_box.get("secret", "")
            verified.append(verify(secret, self.headers.get(SIGNATURE_HEADER, ""), body))
            reply = json.dumps({"protocol_version": "1"}).encode()
            self.send_response(200)
            self.send_header("content-length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/agent", secret_box, verified
    finally:
        server.shutdown()
        server.server_close()


async def _http_config(owner: Caller, url: str) -> str:
    response = await owner.post(
        "/api/v1/agent-configs",
        json={"name": "external", "adapter_type": "http", "settings": {"url": url}},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def test_only_owners_create_the_secret_and_it_is_shown_once(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    member = await token_caller(owner, clients(), role="member")
    before = (await owner.get("/api/v1/agent-signing-secret")).json()
    refused = await member.post("/api/v1/agent-signing-secret")
    first = await owner.post("/api/v1/agent-signing-secret")
    second = await owner.post("/api/v1/agent-signing-secret")
    after = (await member.get("/api/v1/agent-signing-secret")).json()
    assert before == {"configured": False, "last4": None, "created_at": None}
    assert refused.status_code == 403
    secret = first.json()["secret"]
    assert first.status_code == 201 and secret.startswith("whsec_")
    assert second.json()["secret"] != secret
    assert after["configured"] and after["last4"] == second.json()["secret"][-4:]
    assert "secret" not in after


async def test_a_real_experiment_with_an_http_agent_needs_the_secret(new_owner: SignIn) -> None:
    owner = await new_owner()
    imported = await owner.post("/api/v1/datasets/import", json={"name": "r", "tasks": [TASK]})
    config = await _http_config(owner, "https://agent.example/run")
    body = {
        "dataset_version_id": imported.json()["dataset_version_id"],
        "agent_config_ids": [config],
        "mode": "real",
    }
    missing = await owner.post("/api/v1/experiments", json=body)
    await owner.post("/api/v1/agent-signing-secret")
    ready = await owner.post("/api/v1/experiments", json=body)
    assert (missing.status_code, missing.json()["error"]) == (409, "signing_secret_required")
    assert ready.status_code == 201


async def test_check_sends_a_signed_handshake(
    new_owner: SignIn, app: FastAPI, handshake_server: tuple[str, dict[str, str], list[bool]]
) -> None:
    url, secret_box, verified = handshake_server
    owner = await new_owner()
    config = await _http_config(owner, url)
    secret_box["secret"] = (await owner.post("/api/v1/agent-signing-secret")).json()["secret"]
    strict = await owner.post(f"/api/v1/agent-configs/{config}/check")
    assert strict.json()["ok"] is False
    assert strict.json()["problem"].startswith("blocked")
    app.state.settings = app.state.settings.model_copy(update={"allow_local_agents": True})
    checked = await owner.post(f"/api/v1/agent-configs/{config}/check")
    assert checked.status_code == 200
    assert checked.json()["ok"] is True
    # The agent verified the request with the secret the owner was shown once.
    assert verified == [True]


async def test_only_http_agents_can_be_checked(new_owner: SignIn) -> None:
    owner = await new_owner()
    scripted = await owner.post(
        "/api/v1/agent-configs",
        json={
            "name": "s",
            "adapter_type": "scripted",
            "settings": {"script": {"steps": [], "final_response": "x"}},
        },
    )
    response = await owner.post(f"/api/v1/agent-configs/{scripted.json()['id']}/check")
    assert (response.status_code, response.json()["detail"]["error"]) == (422, "not_an_http_agent")
