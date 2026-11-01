from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from app.api.main import create_app
from app.settings import Settings


async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(create_app(settings)):
        yield c


@pytest.fixture
async def client_without_db(unreachable_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(create_app(unreachable_settings)):
        yield c


async def test_live_is_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_when_database_is_migrated(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok", "migrations": "at_head"}


async def test_live_does_not_depend_on_database(client_without_db: httpx.AsyncClient) -> None:
    response = await client_without_db.get("/health/live")
    assert response.status_code == 200


async def test_not_ready_when_database_is_unreachable(
    client_without_db: httpx.AsyncClient,
) -> None:
    response = await client_without_db.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "database": "unreachable",
        "migrations": "unknown",
    }


async def test_not_ready_when_migrations_are_behind(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.db.schema_status.expected_heads", lambda: ["abc123"])
    response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["migrations"] == "behind"
