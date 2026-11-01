from collections.abc import Callable

import httpx

from app.api.dependencies import CSRF_HEADER
from tests.api.conftest import SignIn, token_caller


async def test_no_credentials_is_401(clients: Callable[[], httpx.AsyncClient]) -> None:
    response = await clients().get("/api/v1/workspaces")
    assert (response.status_code, response.json()["detail"]) == (
        401,
        {"error": "not_authenticated"},
    )


async def test_bad_bearer_token_is_401(clients: Callable[[], httpx.AsyncClient]) -> None:
    response = await clients().get(
        "/api/v1/workspaces", headers={"authorization": "Bearer aet_made_up"}
    )
    assert response.status_code == 401


async def test_cookie_requests_that_change_things_need_the_csrf_token(new_owner: SignIn) -> None:
    owner = await new_owner()
    body = {"name": "x", "role": "viewer"}
    missing = await owner.client.post("/api/v1/api-tokens", json=body)
    wrong = await owner.client.post(
        "/api/v1/api-tokens", json=body, headers={CSRF_HEADER: "csrf_wrong"}
    )
    assert missing.status_code == wrong.status_code == 403
    assert (await owner.post("/api/v1/api-tokens", json=body)).status_code == 201
    assert (await owner.client.get("/api/v1/api-tokens")).status_code == 200


async def test_token_requests_need_no_csrf_token(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    bot = await token_caller(owner, clients(), role="owner")
    assert (
        await bot.post("/api/v1/api-tokens", json={"name": "y", "role": "viewer"})
    ).status_code == 201


async def test_roles_gate_routes(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    member = await token_caller(owner, clients(), role="member")
    response = await member.get("/api/v1/api-tokens")
    assert (response.status_code, response.json()["detail"]) == (403, {"error": "forbidden"})
