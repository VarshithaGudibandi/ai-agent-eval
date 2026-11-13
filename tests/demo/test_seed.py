from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.api.main import create_app
from app.db import models
from app.demo.seed import LIVE_DAYS, LIVE_PER_DAY, _recorded, live_traffic, seed
from app.settings import Settings
from tests.api.conftest import PASSWORD

CAUSE = "refund orders straight away"


@pytest.fixture
async def demo_app(settings: Settings, fresh_database_url: str) -> AsyncIterator[FastAPI]:
    app = create_app(
        settings.model_copy(update={"database_url": fresh_database_url, "demo_mode": True})
    )
    async with app.router.lifespan_context(app):
        yield app


async def _count(app: FastAPI, model: type[models.Base]) -> int:
    async with app.state.sessionmaker() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_the_demo_loads_once_and_shows_a_regression_caught_and_explained(
    demo_app: FastAPI,
) -> None:
    first = await seed(demo_app.state.sessionmaker, demo_app.state.settings)
    traces = await _count(demo_app, models.Trace)
    assert await seed(demo_app.state.sessionmaker, demo_app.state.settings) == first
    assert await _count(demo_app, models.Trace) == traces == 80 + LIVE_DAYS * LIVE_PER_DAY
    async with demo_app.state.sessionmaker() as session:
        comparison = await session.scalar(select(models.Comparison))
        report = await session.scalar(select(models.RegressionReport))
        run_ids = set(await session.scalars(select(models.Run.id)))
        stored = dict(
            (
                await session.execute(
                    select(models.AgentVersion.label, models.AgentVersion.created_at)
                )
            ).all()
        )
    # The pages take the newest version as the current one.
    assert stored["v2"] > stored["v1"]
    assert comparison is not None and comparison.decision == "hold"
    assert report is not None and report.status == "done"
    cited = {s["run_id"] for g in report.report["groups"] for c in g["claims"] for s in c["spans"]}
    assert cited and cited <= run_ids

    transport = httpx.ASGITransport(app=demo_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as visitor:
        assert "Try the demo" in (await visitor.get("/login")).text
        signed_in = await visitor.post("/api/v1/auth/demo")
        assert signed_in.status_code == 200, signed_in.text
        assert signed_in.json()["role"] == "viewer"
        [agent] = (await visitor.get("/api/v1/agents")).json()
        page = (await visitor.get(f"/agents/{agent['id']}")).text
        assert "Demo data" in page, "banner"
        assert CAUSE in page, page[page.find("s2") : page.find("s4")]
        assert "likely real" in page, "real"
        # A viewer looks and never changes anything.
        csrf = {"x-csrf-token": signed_in.json()["csrf_token"]}
        attempt = await visitor.post(
            "/api/v1/datasets", json={"name": "x", "cases": []}, headers=csrf
        )
        assert attempt.status_code == 403

    # The demo's accounts do not count as an install already set up.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as newcomer:
        body = {"email": "owner@example.com", "password": PASSWORD, "workspace_name": "Team"}
        assert (await newcomer.post("/api/v1/auth/setup", json=body)).status_code == 201


async def test_without_demo_mode_there_is_no_demo_sign_in(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as visitor:
        assert (await visitor.post("/api/v1/auth/demo")).status_code == 404
        assert "Try the demo" not in (await visitor.get("/login")).text


def test_live_traffic_switches_to_v2_when_it_shipped_and_ends_before_now() -> None:
    now = datetime(2026, 11, 13, 12, tzinfo=UTC)
    recorded = _recorded()
    traffic = live_traffic(recorded, now)
    prompts = {label: recorded[label][0]["config"]["prompt"] for label in ("v1", "v2")}
    first_days = [t for t in traffic if t["trace_id"].startswith(f"live-{LIVE_DAYS}-")]
    last_day = [t for t in traffic if t["trace_id"].startswith("live-1-")]
    assert {t["config"]["prompt"] for t in first_days} == {prompts["v1"]}
    assert {t["config"]["prompt"] for t in last_day} == {prompts["v2"]}
    ends = [datetime.fromisoformat(s["end"]) for t in traffic for s in t["spans"]]
    assert max(ends) < now and min(ends) > now - timedelta(days=LIVE_DAYS + 1)
    assert all(t["tags"] == {"environment": "production"} for t in traffic)
