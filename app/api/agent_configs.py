from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, ValidationError

from app.agents.contract import RunLimits
from app.agents.factory import UnsupportedAdapterError, validate_settings
from app.agents.tool_loop import ToolLoopSettings
from app.api.dependencies import Db, Member, Viewer, scope_for
from app.db import models
from app.db.audit import record_audit
from app.db.content import create_agent_config, list_agent_configs

router = APIRouter(prefix="/api/v1/agent-configs", tags=["agent configs"])


class NewConfigRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    adapter_type: models.AdapterType
    settings: dict[str, Any]
    limits: RunLimits = RunLimits()


class ConfigOut(BaseModel):
    id: str
    name: str
    adapter_type: models.AdapterType
    settings: dict[str, Any]
    limits: RunLimits
    config_hash: str
    created_at: datetime

    @classmethod
    def of(cls, row: models.AgentConfig) -> "ConfigOut":
        return cls(
            id=row.id,
            name=row.name,
            adapter_type=models.AdapterType(row.adapter_type),
            settings=row.settings,
            limits=RunLimits.model_validate(row.limits),
            config_hash=row.config_hash,
            created_at=row.created_at,
        )


@router.post("", status_code=status.HTTP_201_CREATED)
async def new_config(
    body: NewConfigRequest, request: Request, principal: Member, session: Db
) -> ConfigOut:
    """Register an immutable agent config. Settings are checked by building the adapter."""
    try:
        validate_settings(body.adapter_type, body.settings)
    except UnsupportedAdapterError:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "adapter_unsupported",
                "message": f"{body.adapter_type} is not available yet",
            },
        ) from None
    except (ValidationError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=422, detail={"error": "settings_invalid", "message": str(exc)}
        ) from None
    if body.adapter_type == models.AdapterType.TOOL_LOOP:
        request.app.state.providers.get(ToolLoopSettings.model_validate(body.settings).provider)
    scope = scope_for(session, principal)
    row = await create_agent_config(scope, body.name, body.adapter_type, body.settings, body.limits)
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "agent_config.created",
        "agent_config",
        row.id,
    )
    await session.refresh(row)
    return ConfigOut.of(row)


@router.get("")
async def configs(principal: Viewer, session: Db) -> list[ConfigOut]:
    """Agent configs in this workspace, newest first."""
    return [ConfigOut.of(c) for c in await list_agent_configs(scope_for(session, principal))]
