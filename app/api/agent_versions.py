from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import Db, Member, Viewer, scope_for
from app.db import models
from app.db.audit import record_audit
from app.db.scope import WorkspaceScope
from app.db.traces import previous_version
from app.explain.diff import config_diff
from app.explain.report import ChangeOut

router = APIRouter(prefix="/api/v1/agent-versions", tags=["agents"])


class VersionDetailOut(BaseModel):
    id: str
    agent_id: str
    fingerprint: str
    label: str | None
    created_at: datetime
    config: dict[str, Any]
    # The version seen just before, and what changed since it. None for the first version.
    previous_version_id: str | None
    changes: list[ChangeOut]


class LabelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None clears the label.
    label: str | None = Field(min_length=1, max_length=100)


async def _out(scope: WorkspaceScope, version: models.AgentVersion) -> VersionDetailOut:
    previous = await previous_version(scope, version)
    changes = config_diff(previous.config, version.config) if previous else []
    return VersionDetailOut(
        id=version.id,
        agent_id=version.agent_id,
        fingerprint=version.fingerprint,
        label=version.label,
        created_at=version.created_at,
        config=version.config,
        previous_version_id=previous.id if previous else None,
        changes=[
            ChangeOut(field=c.field, summary=c.summary, detail=list(c.detail)) for c in changes
        ],
    )


@router.get("/{version_id}")
async def version(version_id: str, principal: Viewer, session: Db) -> VersionDetailOut:
    """One agent version: its config and what changed since the version before it."""
    scope = scope_for(session, principal)
    return await _out(scope, await scope.require(models.AgentVersion, version_id))


@router.patch("/{version_id}")
async def set_label(
    version_id: str, body: LabelIn, principal: Member, session: Db
) -> VersionDetailOut:
    """Give a version a name people use, such as "v7"."""
    scope = scope_for(session, principal)
    row = await scope.require(models.AgentVersion, version_id)
    row.label = body.label
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "agent_version.labelled",
        "agent_version",
        row.id,
        {"label": body.label},
    )
    await scope.flush()
    return await _out(scope, row)
