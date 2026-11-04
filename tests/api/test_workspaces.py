from collections.abc import Callable

import httpx

from app.db.ids import new_id
from tests.api.conftest import PASSWORD, SignIn, sign_in, token_caller


async def test_invite_flow_adds_a_viewer_who_can_read_but_not_write(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    invite = await owner.post(
        f"/api/v1/workspaces/{owner.workspace_id}/invites", json={"role": "viewer"}
    )
    assert invite.status_code == 201
    guest_client = clients()
    email = f"{new_id('guest')}@example.com"
    accepted = await guest_client.post(
        "/api/v1/invites/accept",
        json={"token": invite.json()["token"], "email": email, "password": PASSWORD},
    )
    assert accepted.status_code == 200
    assert accepted.json()["role"] == "viewer"
    guest = await sign_in(guest_client, email, owner.workspace_id)
    listed = await guest.get("/api/v1/workspaces")
    assert [w["role"] for w in listed.json()] == ["viewer"]
    refused = await guest.post(
        "/api/v1/workspaces/" + owner.workspace_id + "/invites", json={"role": "viewer"}
    )
    assert refused.status_code == 403
    members = (await guest.get("/api/v1/members")).json()
    assert [(m["email"], m["role"]) for m in members] == [(owner.email, "owner"), (email, "viewer")]


async def test_existing_user_must_give_their_password_to_accept(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner, other = await new_owner(), await new_owner()
    invite = await owner.post(
        f"/api/v1/workspaces/{owner.workspace_id}/invites", json={"role": "member"}
    )
    response = await clients().post(
        "/api/v1/invites/accept",
        json={
            "token": invite.json()["token"],
            "email": other.email,
            "password": "wrong password!!",
        },
    )
    assert response.status_code == 401
    ok = await clients().post(
        "/api/v1/invites/accept",
        json={"token": invite.json()["token"], "email": other.email, "password": PASSWORD},
    )
    assert ok.status_code == 200


async def test_new_workspace_belongs_to_its_creator(new_owner: SignIn) -> None:
    owner = await new_owner()
    created = await owner.post("/api/v1/workspaces", json={"name": "second"})
    assert created.status_code == 201
    roles = {w["id"]: w["role"] for w in (await owner.get("/api/v1/workspaces")).json()}
    assert roles == {owner.workspace_id: "owner", created.json()["id"]: "owner"}


async def test_tokens_cannot_create_workspaces_or_invites(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    bot = await token_caller(owner, clients(), role="owner")
    assert (await bot.post("/api/v1/workspaces", json={"name": "x"})).status_code == 403
    response = await bot.post(
        f"/api/v1/workspaces/{bot.workspace_id}/invites", json={"role": "viewer"}
    )
    assert (response.status_code, response.json()["error"]) == (403, "user_required")
    assert [w["id"] for w in (await bot.get("/api/v1/workspaces")).json()] == [owner.workspace_id]
