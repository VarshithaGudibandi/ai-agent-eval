from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from app.api.dependencies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from app.api.main import create_app
from app.settings import Settings
from tests.api.conftest import PASSWORD, SignIn


@pytest.fixture
async def fresh_client(
    settings: Settings, fresh_database_url: str
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings.model_copy(update={"database_url": fresh_database_url}))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_setup_makes_the_first_owner_once_then_they_can_sign_in(
    fresh_client: httpx.AsyncClient,
) -> None:
    body = {"email": "owner@example.com", "password": PASSWORD, "workspace_name": "Team"}
    first = await fresh_client.post("/api/v1/auth/setup", json=body)
    assert first.status_code == 201
    again = await fresh_client.post("/api/v1/auth/setup", json={**body, "email": "x@example.com"})
    assert (again.status_code, again.json()["error"]) == (409, "setup_already_done")
    login = await fresh_client.post(
        "/api/v1/auth/login", json={"email": "owner@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200
    assert login.json()["workspace_id"] == first.json()["workspace_id"]
    assert login.json()["role"] == "owner"


async def test_setup_refuses_short_passwords(fresh_client: httpx.AsyncClient) -> None:
    response = await fresh_client.post(
        "/api/v1/auth/setup",
        json={"email": "o@example.com", "password": "short", "workspace_name": "T"},
    )
    assert response.status_code == 422


async def test_session_cookie_is_http_only_and_same_site(new_owner: SignIn) -> None:
    owner = await new_owner()
    cookie = next(c for c in owner.client.cookies.jar if c.name == SESSION_COOKIE)
    assert cookie.has_nonstandard_attr("HttpOnly")
    assert cookie.get_nonstandard_attr("SameSite", "").lower() == "lax"


async def test_the_csrf_cookie_matches_the_token_and_only_echoing_it_works(
    new_owner: SignIn,
) -> None:
    owner = await new_owner()
    cookie = next(c for c in owner.client.cookies.jar if c.name == CSRF_COOKIE)
    # Our own pages read it to echo it, so it is not HttpOnly; another site cannot read it.
    assert not cookie.has_nonstandard_attr("HttpOnly")
    assert cookie.get_nonstandard_attr("SameSite", "").lower() == "strict"
    assert cookie.value == owner.headers[CSRF_HEADER]
    # The cookie alone proves nothing: a cross-site request carries it but cannot echo it.
    bare = await owner.client.post("/api/v1/api-tokens", json={"name": "x", "role": "viewer"})
    assert bare.status_code == 403
    echoed = await owner.post("/api/v1/api-tokens", json={"name": "x", "role": "viewer"})
    assert echoed.status_code == 201


async def test_wrong_password_and_unknown_email_look_the_same(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    client = clients()
    wrong = await client.post(
        "/api/v1/auth/login", json={"email": owner.email, "password": "not the password"}
    )
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"error": "invalid_credentials"}


async def test_logout_ends_the_session(new_owner: SignIn) -> None:
    owner = await new_owner()
    cookie = owner.client.cookies[SESSION_COOKIE]
    assert (await owner.post("/api/v1/auth/logout")).status_code == 204
    owner.client.cookies.set(SESSION_COOKIE, cookie)
    assert (await owner.get("/api/v1/workspaces")).status_code == 401
