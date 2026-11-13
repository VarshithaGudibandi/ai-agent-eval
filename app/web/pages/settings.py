import json
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from app.api import agents, api_tokens, budgets, provider_credentials, workspaces
from app.api.dependencies import Db
from app.db import models
from app.web.formatting import version_name
from app.web.rendering import Person, render

router = APIRouter(include_in_schema=False)


@router.get("/settings")
async def settings(
    request: Request,
    principal: Person,
    session: Db,
    agent: Annotated[str | None, Query(max_length=100)] = None,
) -> Response:
    is_owner = principal.role == models.Role.OWNER
    found = await agents.agents(principal, session)
    chosen = (
        await agents.agent(agent, principal, session) if agent else (found[0] if found else None)
    )
    spec = (
        await agents.eval_spec(chosen.id, principal, session)
        if chosen and chosen.eval_spec_version
        else None
    )
    return render(
        request,
        "settings.html",
        principal=principal,
        is_owner=is_owner,
        can_edit=principal.role in (models.Role.MEMBER, models.Role.OWNER),
        members=await workspaces.members(principal, session),
        tokens=await api_tokens.list_tokens(principal, session) if is_owner else [],
        keys=await provider_credentials.credentials(principal, session) if is_owner else [],
        budgets=await budgets.budgets(principal, session, limit=20),
        agents=found,
        chosen=chosen,
        names={v.id: version_name(v) for v in chosen.versions} if chosen else {},
        spec=spec,
        # The editor starts from the scorers in force, in the shape the API takes back.
        spec_json=json.dumps(
            [
                {
                    "key": s.key,
                    "check": s.check,
                    "severity": s.severity,
                    "threshold": s.threshold,
                    "params": s.params,
                    "applies_when": s.applies_when,
                }
                for s in (spec.scorers if spec else [])
            ],
            indent=2,
        ),
        gates=await agents.gates(chosen.id, principal, session) if chosen else None,
        schedule=await agents.judge_schedule(chosen.id, principal, session) if chosen else None,
    )
