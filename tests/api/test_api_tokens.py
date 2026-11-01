from collections.abc import Callable

import httpx

from tests.api.conftest import SignIn, token_caller


async def test_token_secret_is_shown_once_and_revoking_stops_it(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    bot = await token_caller(owner, clients(), role="viewer")
    secret = bot.headers["authorization"].removeprefix("Bearer ")
    listed = (await owner.get("/api/v1/api-tokens")).json()
    assert [t["revoked"] for t in listed] == [False]
    assert secret not in str(listed)
    assert (await bot.get("/api/v1/workspaces")).status_code == 200
    assert (await owner.delete(f"/api/v1/api-tokens/{listed[0]['id']}")).status_code == 204
    assert (await bot.get("/api/v1/workspaces")).status_code == 401


async def test_unknown_token_id_is_404(new_owner: SignIn) -> None:
    owner = await new_owner()
    assert (await owner.delete("/api/v1/api-tokens/tok_missing")).status_code == 404
