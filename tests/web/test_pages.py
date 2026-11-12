from collections.abc import Callable

import httpx

from tests.api.conftest import SignIn, token_caller


async def test_pages_need_a_signed_in_person(clients: Callable[[], httpx.AsyncClient]) -> None:
    client = clients()
    response = await client.get("/settings")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/settings"
    login = await client.get("/login")
    assert login.status_code == 200
    assert "script-src 'self'" in login.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in login.headers["content-security-policy"]


async def test_home_leads_to_settings(new_owner: SignIn) -> None:
    owner = await new_owner()
    response = await owner.get("/")
    assert (response.status_code, response.headers["location"]) == (303, "/settings")


async def test_settings_show_owner_tools_only_to_owners(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    page = await owner.get("/settings")
    assert page.status_code == 200
    assert "API tokens" in page.text and "Provider keys" in page.text
    assert str(owner.email) in page.text
    invite = await owner.post(
        f"/api/v1/workspaces/{owner.workspace_id}/invites", json={"role": "viewer"}
    )
    viewer_client = clients()
    accepted = await viewer_client.post(
        "/api/v1/invites/accept",
        json={
            "token": invite.json()["token"],
            "email": "viewer@example.com",
            "password": "a long enough password",
        },
    )
    assert accepted.status_code == 200
    await viewer_client.post(
        "/api/v1/auth/login",
        json={
            "email": "viewer@example.com",
            "password": "a long enough password",
            "workspace_id": owner.workspace_id,
        },
    )
    seen = await viewer_client.get("/settings")
    assert seen.status_code == 200
    assert "API tokens" not in seen.text and "Provider keys" not in seen.text


async def test_api_tokens_cannot_open_pages(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    bot = await token_caller(owner, clients(), role="owner")
    assert (await bot.get("/settings")).status_code == 303


async def test_static_files_are_served(clients: Callable[[], httpx.AsyncClient]) -> None:
    client = clients()
    for path in ("/static/app.js", "/static/style.css"):
        assert (await client.get(path)).status_code == 200, path
