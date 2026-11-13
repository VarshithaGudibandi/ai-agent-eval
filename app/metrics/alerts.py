from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.db.judge_batches import schedule_of
from app.db.scope import WorkspaceScope
from app.metrics.agent_health import WINDOW, live_facts, live_windows
from app.metrics.comparison import Gates, compare

# Alerts are worked out when asked for, never stored or sent: the last week of live runs
# against the week before, through the agent's own gates. A gate trips only on a change the
# interval says is real, so a quiet agent cannot raise an alert from noise.


class Alert(BaseModel):
    kind: Literal["gate", "schedule"]
    message: str


class AlertsOut(BaseModel):
    start: datetime
    end: datetime
    alerts: list[Alert]


async def agent_alerts(
    scope: WorkspaceScope, agent_id: str, gates: Gates, end: datetime
) -> AlertsOut:
    previous, current = await live_windows(scope, agent_id, end)
    alerts = []
    if previous.outcomes and current.outcomes:
        result = compare(
            previous.outcomes,
            current.outcomes,
            live_facts(previous, "previous week"),
            live_facts(current, "last week"),
            gates,
            paired=False,
            seed=0,
            bootstrap=False,
        )
        if result.decision == "hold":
            alerts += [Alert(kind="gate", message=f"Last week: {r}") for r in result.reasons]
    schedule = await schedule_of(scope, agent_id)
    if schedule is not None and schedule.last_error:
        alerts.append(
            Alert(
                kind="schedule",
                message=f"The judge schedule could not start a batch: {schedule.last_error}",
            )
        )
    return AlertsOut(start=end - WINDOW, end=end, alerts=alerts)
