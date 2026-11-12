from collections.abc import Callable

import httpx
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.orm import Session
from starlette.types import Message, Receive, Scope, Send

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


async def test_a_write_is_committed_before_the_response_goes_out(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    order: list[str] = []

    def committed(session: Session) -> None:
        order.append("commit")

    async def recording(scope: Scope, receive: Receive, send: Send) -> None:
        async def watched(message: Message) -> None:
            if message["type"] == "http.response.start":
                order.append("response")
            await send(message)

        await app(scope, receive, watched)

    event.listen(Session, "after_commit", committed)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=recording),
            base_url="http://test",
            cookies=owner.client.cookies,
        ) as client:
            response = await client.post(
                "/api/v1/api-tokens", json={"name": "ci", "role": "viewer"}, headers=owner.headers
            )
    finally:
        event.remove(Session, "after_commit", committed)
    assert response.status_code == 201
    # A client that gets 201 can rely on the row being there for its very next request.
    assert order.index("commit") < order.index("response")
