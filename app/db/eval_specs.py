from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from app.db import models
from app.db.ids import new_id
from app.db.scope import NotFoundError, WorkspaceScope


@dataclass(frozen=True)
class NewScorer:
    key: str
    check: str
    kind: str
    scope: str
    severity: str
    threshold: float
    params: dict[str, Any]
    applies_when: dict[str, Any]
    content_hash: str


async def _lock_agent(scope: WorkspaceScope, agent_id: str) -> models.Agent:
    # Saves for one agent take turns, so two can never claim the same version number.
    agent = await scope.session.scalar(
        scope.query(models.Agent).where(models.Agent.id == agent_id).with_for_update()
    )
    if agent is None:
        raise NotFoundError(f"agents:{agent_id}")
    return agent


async def current_spec(scope: WorkspaceScope, agent_id: str) -> models.EvalSpec | None:
    return await scope.first(
        scope.query(models.EvalSpec)
        .where(models.EvalSpec.agent_id == agent_id)
        .order_by(models.EvalSpec.version.desc())
    )


async def spec_version(scope: WorkspaceScope, agent_id: str, version: int) -> models.EvalSpec:
    spec = await scope.first(
        scope.query(models.EvalSpec).where(
            models.EvalSpec.agent_id == agent_id, models.EvalSpec.version == version
        )
    )
    if spec is None:
        raise NotFoundError(f"eval_specs:{agent_id}@{version}")
    return spec


async def spec_at(scope: WorkspaceScope, agent_id: str, when: datetime) -> models.EvalSpec | None:
    """The spec version that was in force at that moment."""
    return await scope.first(
        scope.query(models.EvalSpec)
        .where(models.EvalSpec.agent_id == agent_id, models.EvalSpec.created_at <= when)
        .order_by(models.EvalSpec.version.desc())
    )


async def scorers_of(scope: WorkspaceScope, spec_id: str) -> list[models.Scorer]:
    return await scope.all(
        scope.query(models.Scorer)
        .join(models.EvalSpecScorer, models.EvalSpecScorer.scorer_id == models.Scorer.id)
        .where(models.EvalSpecScorer.eval_spec_id == spec_id)
        .order_by(models.EvalSpecScorer.position)
    )


async def _scorer_version(scope: WorkspaceScope, agent_id: str, scorer: NewScorer) -> models.Scorer:
    existing = await scope.first(
        scope.query(models.Scorer).where(
            models.Scorer.agent_id == agent_id,
            models.Scorer.key == scorer.key,
            models.Scorer.content_hash == scorer.content_hash,
        )
    )
    if existing is not None:
        return existing
    latest = await scope.session.scalar(
        select(func.max(models.Scorer.version)).where(
            models.Scorer.workspace_id == scope.workspace_id,
            models.Scorer.agent_id == agent_id,
            models.Scorer.key == scorer.key,
        )
    )
    row = models.Scorer(
        id=new_id("scr"),
        workspace_id=scope.workspace_id,
        agent_id=agent_id,
        version=(latest or 0) + 1,
        **vars(scorer),
    )
    scope.add(row)
    await scope.flush()
    return row


async def save_spec(
    scope: WorkspaceScope, agent_id: str, scorers: list[NewScorer]
) -> tuple[models.EvalSpec, bool]:
    """Save the agent's scorers as its next spec version. Returns it and whether it is new.

    Saving the same scorers again returns the current version instead of a copy.
    """
    await _lock_agent(scope, agent_id)
    current = await current_spec(scope, agent_id)
    if current is not None:
        held = [(s.key, s.content_hash) for s in await scorers_of(scope, current.id)]
        if held == [(s.key, s.content_hash) for s in scorers]:
            return current, False
    spec = models.EvalSpec(
        id=new_id("spec"),
        workspace_id=scope.workspace_id,
        agent_id=agent_id,
        version=(current.version if current else 0) + 1,
    )
    scope.add(spec)
    await scope.flush()
    for position, scorer in enumerate(scorers):
        row = await _scorer_version(scope, agent_id, scorer)
        scope.add(
            models.EvalSpecScorer(
                workspace_id=scope.workspace_id,
                eval_spec_id=spec.id,
                scorer_id=row.id,
                position=position,
            )
        )
    await scope.flush()
    return spec, True


async def ensure_spec(scope: WorkspaceScope, agent_id: str, scorers: list[NewScorer]) -> None:
    """Give an agent its first spec, unless it has one.

    Called for every trace, so the usual case is one indexed read and no lock.
    """
    if await current_spec(scope, agent_id) is not None:
        return
    await _lock_agent(scope, agent_id)
    # Another first trace of the same agent may have created it while this one waited.
    if await current_spec(scope, agent_id) is None:
        await save_spec(scope, agent_id, scorers)
