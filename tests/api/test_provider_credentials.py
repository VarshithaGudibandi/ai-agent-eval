from collections.abc import Callable

import httpx
from fastapi import FastAPI

from tests.api.conftest import SignIn, token_caller

KEY = {"provider": "gemini", "label": "dev", "api_key": "AIzaSyFAKEFAKEFAKE-9876"}


async def test_key_is_stored_encrypted_and_never_returned(new_owner: SignIn) -> None:
    owner = await new_owner()
    created = await owner.post("/api/v1/provider-credentials", json=KEY)
    assert created.status_code == 201
    body = created.json()
    assert (body["last4"], body["provider"]) == ("9876", "gemini")
    assert "free-tier" in body["warning"]
    assert "AIzaSyFAKE" not in created.text
    listed = await owner.get("/api/v1/provider-credentials")
    assert "AIzaSyFAKE" not in listed.text
    assert (await owner.delete(f"/api/v1/provider-credentials/{body['id']}")).status_code == 204
    assert [c["revoked"] for c in (await owner.get("/api/v1/provider-credentials")).json()] == [
        True
    ]


async def test_members_cannot_manage_keys(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    member = await token_caller(owner, clients(), role="member")
    assert (await member.get("/api/v1/provider-credentials")).status_code == 403
    assert (await member.post("/api/v1/provider-credentials", json=KEY)).status_code == 403


async def test_missing_master_key_gives_a_clear_error(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    app.state.settings = app.state.settings.model_copy(update={"app_master_key": None})
    response = await owner.post("/api/v1/provider-credentials", json=KEY)
    assert (response.status_code, response.json()["detail"]["error"]) == (503, "master_key_missing")
