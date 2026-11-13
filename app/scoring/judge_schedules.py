import logging
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.ids import new_id
from app.db.judge_batches import (
    due_schedules,
    live_runs_of_version,
    new_version_batch,
    schedule_of,
)
from app.db.scope import NotFoundError, WorkspaceScope
from app.scoring.definitions import Tag
from app.scoring.judge_batches import (
    MAX_BATCH,
    JudgingRefusedError,
    NewJudgeBatch,
    start_batch,
)

logger = logging.getLogger(__name__)


class ScheduleIn(BaseModel):
    """When to judge an agent's live runs without being asked."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # Monday is 0; leave out to run every day. Times are UTC.
    weekday: int | None = Field(default=None, ge=0, le=6)
    hour: int = Field(default=9, ge=0, le=23)
    # Judge the runs of the last this many days.
    window_days: int = Field(default=7, ge=1, le=90)
    limit: int = Field(default=200, ge=1, le=MAX_BATCH)
    tags: dict[str, Tag] = Field(default_factory=dict, max_length=20)
    # Judge each new version once it has this many live runs. None turns this off.
    new_version_runs: int | None = Field(default=None, ge=1, le=MAX_BATCH)


def next_occurrence(after: datetime, weekday: int | None, hour: int) -> datetime:
    """The first time strictly after `after` that is the given hour, on the given weekday."""
    moment = after.astimezone(UTC).replace(minute=0, second=0, microsecond=0, hour=hour)
    while moment <= after or (weekday is not None and moment.weekday() != weekday):
        moment += timedelta(days=1)
    return moment


async def save_schedule(
    scope: WorkspaceScope, agent_id: str, body: ScheduleIn
) -> models.JudgeSchedule:
    await scope.require(models.Agent, agent_id)
    schedule = await schedule_of(scope, agent_id)
    if schedule is None:
        schedule = models.JudgeSchedule(
            id=new_id("js"), workspace_id=scope.workspace_id, agent_id=agent_id
        )
        scope.add(schedule)
    schedule.enabled = body.enabled
    schedule.weekday = body.weekday
    schedule.hour = body.hour
    schedule.window_days = body.window_days
    schedule.run_limit = body.limit
    schedule.tags = body.tags
    schedule.new_version_runs = body.new_version_runs
    schedule.next_run_at = (
        next_occurrence(datetime.now(UTC), body.weekday, body.hour) if body.enabled else None
    )
    schedule.last_error = None
    await scope.flush()
    return schedule


async def _start(
    scope: WorkspaceScope,
    schedule: models.JudgeSchedule,
    request: NewJudgeBatch,
    trigger: models.JudgeBatchTrigger,
) -> bool:
    """Start a batch for the schedule. A refusal, such as no paid key, is kept on the
    schedule for people to see, never raised: nobody is waiting on this call."""
    try:
        async with scope.session.begin_nested():
            await start_batch(scope, request, trigger)
    except (JudgingRefusedError, NotFoundError) as exc:
        schedule.last_error = str(exc)[:500]
        return False
    schedule.last_error = None
    return True


async def start_due_batches(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    """Start every scheduled batch whose time has come. The worker calls this regularly."""
    started = 0
    async with sessionmaker.begin() as session:
        for schedule in await due_schedules(session, limit=20):
            now = datetime.now(UTC)
            request = NewJudgeBatch(
                agent_id=schedule.agent_id,
                start=now - timedelta(days=schedule.window_days),
                end=now,
                tags=schedule.tags,
                limit=schedule.run_limit,
            )
            scope = WorkspaceScope(session, schedule.workspace_id)
            started += await _start(scope, schedule, request, models.JudgeBatchTrigger.SCHEDULE)
            schedule.next_run_at = next_occurrence(now, schedule.weekday, schedule.hour)
    return started


async def judge_new_version(scope: WorkspaceScope, run: models.Run, agent_id: str) -> None:
    """Once a version has its first N live runs, judge those runs, once."""
    if run.experiment_id is not None:
        return
    schedule = await schedule_of(scope, agent_id)
    if schedule is None or not schedule.enabled or schedule.new_version_runs is None:
        return
    if await new_version_batch(scope, run.agent_version_id) is not None:
        return
    if await live_runs_of_version(scope, run.agent_version_id) < schedule.new_version_runs:
        return
    version = await scope.require(models.AgentVersion, run.agent_version_id)
    # Every run of the version, whenever the agent's clock says it ran.
    request = NewJudgeBatch(
        agent_id=agent_id,
        version_id=version.id,
        start=datetime(1970, 1, 1, tzinfo=UTC),
        end=datetime.now(UTC) + timedelta(days=1),
        limit=schedule.new_version_runs,
    )
    try:
        await _start(scope, schedule, request, models.JudgeBatchTrigger.NEW_VERSION)
    except IntegrityError:
        # Another run of the same version started it a moment ago.
        logger.info("new version batch already started", extra={"version": version.id})
