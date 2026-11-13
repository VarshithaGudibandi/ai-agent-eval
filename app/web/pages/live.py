from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.api import agents, reviews
from app.api.dependencies import Db
from app.db import models
from app.web.formatting import version_name
from app.web.rendering import Person, render

router = APIRouter(include_in_schema=False)

DAYS = 14


@dataclass(frozen=True)
class TrafficDay:
    day: date
    runs: int
    scored: int
    # Bar height as a share of the busiest day.
    height: float


@router.get("/agents/{agent_id}/live")
async def live(request: Request, agent_id: str, principal: Person, session: Db) -> Response:
    agent = await agents.agent(agent_id, principal, session)
    daily = await agents.daily_health(agent_id, principal, session, days=DAYS)
    today = datetime.now(UTC).date()
    totals: dict[date, tuple[int, int]] = {}
    for d in daily:
        runs, scored = totals.get(d.day, (0, 0))
        totals[d.day] = (runs + d.runs, scored + d.health.runs_scored)
    busiest = max((runs for runs, _ in totals.values()), default=0) or 1
    traffic = [
        TrafficDay(day, *totals.get(day, (0, 0)), totals.get(day, (0, 0))[0] / busiest * 100)
        for day in (today - timedelta(days=n) for n in range(DAYS - 1, -1, -1))
    ]
    return render(
        request,
        "live.html",
        principal=principal,
        can_edit=principal.role in (models.Role.MEMBER, models.Role.OWNER),
        agent=agent,
        names={v.id: version_name(v) for v in agent.versions},
        traffic=traffic,
        total_runs=sum(t.runs for t in traffic),
        total_scored=sum(t.scored for t in traffic),
        days=sorted(daily, key=lambda d: (d.day, d.agent_version_id), reverse=True),
        alerts=await agents.alerts(agent_id, principal, session),
        queue=await reviews.review_queue(principal, session, limit=10, agent_id=agent_id),
    )
