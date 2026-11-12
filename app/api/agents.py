from datetime import datetime
from typing import Any

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from app.api.dependencies import Db, Member, Viewer, scope_for
from app.db import models
from app.db.audit import record_audit
from app.db.eval_specs import current_spec, require_current_spec, scorers_of, spec_version
from app.db.scope import WorkspaceScope
from app.db.scores import run_ids
from app.db.traces import agents_with_versions
from app.scoring.definitions import EvalSpecDefinition
from app.scoring.job import queue_scoring
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
