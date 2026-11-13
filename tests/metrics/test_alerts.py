from datetime import UTC, datetime

from fastapi import FastAPI
from sqlalchemy import select

from app.db import models
from app.scoring.judge_schedules import start_due_batches
from tests.api.conftest import SignIn
from tests.api.seeding import send_live

END = "2026-11-20T00:00:00Z"
PREVIOUS_WEEK = datetime(2026, 11, 8, tzinfo=UTC)
LAST_WEEK = datetime(2026, 11, 15, tzinfo=UTC)


async def test_a_real_drop_in_live_traffic_trips_the_gates(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    await send_live(owner, app, "prev", PREVIOUS_WEEK, passed=38, failed=2)
    await send_live(owner, app, "last", LAST_WEEK, passed=24, failed=16)
    [agent] = (await owner.get("/api/v1/agents")).json()
    found = (await owner.get(f"/api/v1/agents/{agent['id']}/alerts?end={END}")).json()
    assert [a["kind"] for a in found["alerts"]] == ["gate"]
    assert found["alerts"][0]["message"].startswith("Last week: success rate down 35.0 points")
    assert (found["start"], found["end"]) == ("2026-11-13T00:00:00Z", END)


async def test_too_few_runs_raise_no_alert_however_bad_they_look(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    # 90% then 70%: a big drop, but on ten runs a week it could be noise.
    await send_live(owner, app, "prev", PREVIOUS_WEEK, passed=9, failed=1)
    await send_live(owner, app, "last", LAST_WEEK, passed=7, failed=3)
    [agent] = (await owner.get("/api/v1/agents")).json()
    found = (await owner.get(f"/api/v1/agents/{agent['id']}/alerts?end={END}")).json()
    assert found["alerts"] == []


async def test_a_judge_schedule_that_could_not_start_is_an_alert(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    await send_live(owner, app, "last", LAST_WEEK, passed=3, failed=0)
    [agent] = (await owner.get("/api/v1/agents")).json()
    await owner.put(f"/api/v1/agents/{agent['id']}/judge-schedule", json={"hour": 9})
    async with app.state.sessionmaker.begin() as session:
        schedule = await session.scalar(
            select(models.JudgeSchedule).where(models.JudgeSchedule.agent_id == agent["id"])
        )
        assert schedule is not None
        schedule.next_run_at = datetime(2026, 1, 1, tzinfo=UTC)
    await start_due_batches(app.state.sessionmaker)
    found = (await owner.get(f"/api/v1/agents/{agent['id']}/alerts?end={END}")).json()
    [alert] = found["alerts"]
    assert alert["kind"] == "schedule" and "could not start" in alert["message"]
