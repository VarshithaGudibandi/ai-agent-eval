from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.api import api_tokens, provider_credentials, workspaces
from app.api.dependencies import Db
from app.db import models
from app.web.rendering import Person, render

router = APIRouter(include_in_schema=False)


@router.get("/settings")
async def settings(request: Request, principal: Person, session: Db) -> Response:
    is_owner = principal.role == models.Role.OWNER
    return render(
        request,
        "settings.html",
        principal=principal,
        is_owner=is_owner,
        members=await workspaces.members(principal, session),
        tokens=await api_tokens.list_tokens(principal, session) if is_owner else [],
        keys=await provider_credentials.credentials(principal, session) if is_owner else [],
    )
