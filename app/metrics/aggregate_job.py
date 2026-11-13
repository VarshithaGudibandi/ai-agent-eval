from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from app.db import models
from app.db.jobs import enqueue, require_lease
from app.db.outcomes import live_outcomes, save_aggregate
from app.db.scope import WorkspaceScope
from app.metrics.health import health

if TYPE_CHECKING:
    from app.jobs.worker import JobContext

AGGREGATE_DAY = "aggregate_day"


async def queue_aggregate(scope: WorkspaceScope, run: models.Run) -> None:
    """Rebuild the day a live run belongs to. Many verdicts on one day queue one job."""
    if run.experiment_id is not None:
        return
    day = run.started_at.astimezone(UTC).date()
    await enqueue(
        scope,
        AGGREGATE_DAY,
        f"{run.agent_version_id}:{day.isoformat()}",
        {"agent_version_id": run.agent_version_id, "day": day.isoformat()},
    )


async def aggregate_day_job(ctx: "JobContext") -> None:
    """Work out one version's health for one day of live runs, from their verdicts."""
    claim = ctx.claim
    day = date.fromisoformat(str(claim.payload["day"]))
    start = datetime.combine(day, time(), UTC)
    async with ctx.sessionmaker.begin() as session:
        await require_lease(session, claim.workspace_id, claim.job_id, claim.lease_token)
        scope = WorkspaceScope(session, claim.workspace_id)
        version = await scope.require(models.AgentVersion, str(claim.payload["agent_version_id"]))
        loaded = await live_outcomes(
            scope, version.agent_id, version.id, start, start + timedelta(days=1)
        )
        result = health(loaded.outcomes)
        await save_aggregate(scope, version.id, day, result.runs, result.model_dump(mode="json"))
