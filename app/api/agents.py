from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field

from app.api.dependencies import Db, Member, Viewer, scope_for
from app.db import models
from app.db.audit import record_audit
from app.db.eval_specs import current_spec, require_current_spec, scorers_of, spec_version
from app.db.judge_batches import schedule_of
from app.db.outcomes import daily_aggregates
from app.db.scope import NotFoundError, WorkspaceScope
from app.db.scores import run_ids
from app.db.traces import agents_with_versions
from app.metrics.comparison import Gates
from app.metrics.comparisons import stored_gates
from app.metrics.health import Health
from app.scoring.definitions import EvalSpecDefinition
from app.scoring.job import queue_scoring
from app.scoring.judge_schedules import ScheduleIn, save_schedule
from app.scoring.specs import save_definitions

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


class VersionOut(BaseModel):
    id: str
    fingerprint: str
    label: str | None
    created_at: datetime


class AgentOut(BaseModel):
    id: str
    name: str
    created_at: datetime
    versions: list[VersionOut]
    eval_spec_version: int | None
    shipped_version_id: str | None


class ScorerOut(BaseModel):
    id: str
    key: str
    version: int
    check: str
    kind: str
    scope: str
    severity: str
    threshold: float
    params: dict[str, Any]
    applies_when: dict[str, Any]


class EvalSpecOut(BaseModel):
    id: str
    agent_id: str
    version: int
    created_at: datetime
    scorers: list[ScorerOut]


class RescoreRequest(BaseModel):
    # Only this experiment's runs; all of the agent's runs when left out.
    experiment_id: str | None = Field(default=None, max_length=100)


class RescoreOut(BaseModel):
    eval_spec_version: int
    queued: int
    # Runs already waiting to be scored, which were left as they were.
    already_waiting: int


async def _agents(scope: WorkspaceScope, agent_id: str | None = None) -> list[AgentOut]:
    found = []
    for agent, versions in await agents_with_versions(scope, agent_id):
        spec = await current_spec(scope, agent.id)
        found.append(
            AgentOut(
                id=agent.id,
                name=agent.name,
                created_at=agent.created_at,
                versions=[
                    VersionOut(
                        id=v.id, fingerprint=v.fingerprint, label=v.label, created_at=v.created_at
                    )
                    for v in versions
                ],
                eval_spec_version=spec.version if spec else None,
                shipped_version_id=agent.shipped_version_id,
            )
        )
    return found


async def _spec_out(scope: WorkspaceScope, spec: models.EvalSpec) -> EvalSpecOut:
    return EvalSpecOut(
        id=spec.id,
        agent_id=spec.agent_id,
        version=spec.version,
        created_at=spec.created_at,
        scorers=[
            ScorerOut(
                id=s.id,
                key=s.key,
                version=s.version,
                check=s.check,
                kind=s.kind,
                scope=s.scope,
                severity=s.severity,
                threshold=s.threshold,
                params=s.params,
                applies_when=s.applies_when,
            )
            for s in await scorers_of(scope, spec.id)
        ],
    )


@router.get("")
async def agents(principal: Viewer, session: Db) -> list[AgentOut]:
    """Agents in this workspace, with their versions and current eval spec version."""
    return await _agents(scope_for(session, principal))


@router.get("/{agent_id}")
async def agent(agent_id: str, principal: Viewer, session: Db) -> AgentOut:
    """One agent with its versions."""
    scope = scope_for(session, principal)
    await scope.require(models.Agent, agent_id)
    [found] = await _agents(scope, agent_id)
    return found


@router.get("/{agent_id}/eval-spec")
async def eval_spec(
    agent_id: str, principal: Viewer, session: Db, version: int | None = None
) -> EvalSpecOut:
    """The agent's current eval spec, or an earlier version of it."""
    scope = scope_for(session, principal)
    await scope.require(models.Agent, agent_id)
    if version is not None:
        return await _spec_out(scope, await spec_version(scope, agent_id, version))
    return await _spec_out(scope, await require_current_spec(scope, agent_id))


@router.put("/{agent_id}/eval-spec", status_code=status.HTTP_201_CREATED)
async def save_eval_spec(
    agent_id: str, body: EvalSpecDefinition, response: Response, principal: Member, session: Db
) -> EvalSpecOut:
    """Save the agent's scorers as a new eval spec version.

    New runs are graded by it; earlier runs keep their verdicts until rescored. Saving the
    same scorers again returns the current version and answers 200.
    """
    scope = scope_for(session, principal)
    spec, created = await save_definitions(scope, agent_id, body.scorers)
    if created:
        record_audit(
            scope,
            principal.actor_type,
            principal.actor_id,
            "eval_spec.saved",
            "eval_spec",
            spec.id,
            {"agent_id": agent_id, "version": spec.version},
        )
    else:
        response.status_code = status.HTTP_200_OK
    await scope.flush()
    await session.refresh(spec)
    return await _spec_out(scope, spec)


@router.post("/{agent_id}/rescore")
async def rescore(
    agent_id: str, body: RescoreRequest, principal: Member, session: Db
) -> RescoreOut:
    """Grade the agent's runs again under its current eval spec."""
    scope = scope_for(session, principal)
    await scope.require(models.Agent, agent_id)
    if body.experiment_id is not None:
        await scope.require(models.Experiment, body.experiment_id)
    spec = await require_current_spec(scope, agent_id)
    queued = 0
    runs = await run_ids(scope, agent_id, body.experiment_id)
    for run_id in runs:
        queued += await queue_scoring(scope, run_id, spec.id)
    return RescoreOut(
        eval_spec_version=spec.version, queued=queued, already_waiting=len(runs) - queued
    )


@router.get("/{agent_id}/gates")
async def gates(agent_id: str, principal: Viewer, session: Db) -> Gates:
    """When a new version of the agent is worse enough to hold back."""
    return await stored_gates(scope_for(session, principal), agent_id)


@router.put("/{agent_id}/gates")
async def save_gates(agent_id: str, body: Gates, principal: Member, session: Db) -> Gates:
    """Set the agent's gates. Every comparison and evalctl run uses them unless told otherwise."""
    scope = scope_for(session, principal)
    agent = await scope.require(models.Agent, agent_id)
    agent.gates = body.model_dump(mode="json")
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "agent.gates_changed",
        "agent",
        agent.id,
        agent.gates,
    )
    return body


class ShippedIn(BaseModel):
    version_id: str


@router.post("/{agent_id}/shipped")
async def mark_shipped(agent_id: str, body: ShippedIn, principal: Member, session: Db) -> AgentOut:
    """Mark the version now in production. New versions are compared with it."""
    scope = scope_for(session, principal)
    agent = await scope.require(models.Agent, agent_id)
    version = await scope.require(models.AgentVersion, body.version_id)
    if version.agent_id != agent.id:
        raise NotFoundError(f"agent_versions:{body.version_id}")
    agent.shipped_version_id = version.id
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "agent.version_shipped",
        "agent",
        agent.id,
        {"version_id": version.id},
    )
    await scope.flush()
    [found] = await _agents(scope, agent_id)
    return found


class DayOut(BaseModel):
    agent_version_id: str
    day: date
    runs: int
    health: Health


@router.get("/{agent_id}/health/daily")
async def daily_health(
    agent_id: str,
    principal: Viewer,
    session: Db,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> list[DayOut]:
    """Health of the agent's live runs, per version and per day, for the last days (UTC)."""
    scope = scope_for(session, principal)
    await scope.require(models.Agent, agent_id)
    since = datetime.now(UTC).date() - timedelta(days=days - 1)
    return [
        DayOut(
            agent_version_id=a.agent_version_id,
            day=a.day,
            runs=a.runs,
            health=Health.model_validate(a.health),
        )
        for a in await daily_aggregates(scope, agent_id, since)
    ]


class ScheduleOut(ScheduleIn):
    next_run_at: datetime | None
    # Why the last scheduled batch did not start, such as no paid key.
    last_error: str | None


@router.get("/{agent_id}/judge-schedule")
async def judge_schedule(agent_id: str, principal: Viewer, session: Db) -> ScheduleOut:
    """When the agent's live runs are judged without being asked. Off until it is set."""
    scope = scope_for(session, principal)
    await scope.require(models.Agent, agent_id)
    row = await schedule_of(scope, agent_id)
    if row is None:
        return ScheduleOut(enabled=False, next_run_at=None, last_error=None)
    return ScheduleOut(
        enabled=row.enabled,
        weekday=row.weekday,
        hour=row.hour,
        window_days=row.window_days,
        limit=row.run_limit,
        tags=row.tags,
        new_version_runs=row.new_version_runs,
        next_run_at=row.next_run_at,
        last_error=row.last_error,
    )


@router.put("/{agent_id}/judge-schedule")
async def save_judge_schedule(
    agent_id: str, body: ScheduleIn, principal: Member, session: Db
) -> ScheduleOut:
    """Judge the agent's live runs on a clock, and once for each new version."""
    scope = scope_for(session, principal)
    row = await save_schedule(scope, agent_id, body)
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "judge_schedule.saved",
        "agent",
        agent_id,
        body.model_dump(mode="json"),
    )
    return ScheduleOut(**body.model_dump(), next_run_at=row.next_run_at, last_error=None)
