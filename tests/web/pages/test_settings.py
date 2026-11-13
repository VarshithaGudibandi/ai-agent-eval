from collections.abc import Callable

import httpx

from tests.api.conftest import SignIn


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
