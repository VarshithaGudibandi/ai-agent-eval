from collections.abc import Callable

import httpx

from tests.api.conftest import SignIn, token_caller

PRICE = {
    "provider": "gemini",
    "model": "gemini-flash-lite-latest",
    "input_per_million_micros": 100_000,
    "cached_input_per_million_micros": 25_000,
    "output_per_million_micros": 400_000,
    "other_per_million_micros": 400_000,
}


async def test_owner_adds_prices_and_viewers_read_them(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    created = await owner.post("/api/v1/price-versions", json=PRICE)
    assert created.status_code == 201
    assert created.json()["currency"] == "USD"
    viewer = await token_caller(owner, clients(), role="viewer")
    assert [p["id"] for p in (await viewer.get("/api/v1/price-versions")).json()] == [
        created.json()["id"]
    ]
    assert (await viewer.post("/api/v1/price-versions", json=PRICE)).status_code == 403


async def test_negative_prices_are_rejected(new_owner: SignIn) -> None:
    owner = await new_owner()
    response = await owner.post(
        "/api/v1/price-versions", json={**PRICE, "input_per_million_micros": -1}
    )
    assert response.status_code == 422
