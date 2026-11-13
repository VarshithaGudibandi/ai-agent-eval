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


async def test_home_leads_to_agents(new_owner: SignIn) -> None:
    owner = await new_owner()
    response = await owner.get("/")
    assert (response.status_code, response.headers["location"]) == (303, "/agents")


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
