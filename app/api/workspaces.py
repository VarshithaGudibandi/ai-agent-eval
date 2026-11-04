from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from app.api.auth import EMAIL_PATTERN
from app.api.dependencies import (
    CurrentPrincipal,
    Db,
    Owner,
    Viewer,
    get_settings_dep,
    scope_for,
)
from app.auth.errors import UserRequiredError
from app.auth.passwords import MIN_PASSWORD_LENGTH
from app.auth.users import authenticate, create_user, find_user, user_by_id
from app.auth.workspaces import (
    accept_invite,
    create_invite,
    create_workspace,
    members_of,
    memberships_of,
    workspace_by_id,
)
from app.db.models import Role
from app.db.scope import NotFoundError
from app.settings import Settings

router = APIRouter(prefix="/api/v1", tags=["workspaces"])


class WorkspaceOut(BaseModel):
    id: str
    name: str
    role: Role


class MemberOut(BaseModel):
    user_id: str
    email: str
    role: Role
    joined_at: datetime


class CreateWorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class InviteRequest(BaseModel):
    role: Role


class InviteOut(BaseModel):
    invite_id: str
    token: str
    role: Role
    expires_at: datetime


class AcceptInviteRequest(BaseModel):
    token: str = Field(max_length=200)
    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)


class AcceptInviteOut(BaseModel):
    user_id: str
    workspace_id: str
    role: Role


@router.get("/workspaces")
async def list_workspaces(principal: CurrentPrincipal, session: Db) -> list[WorkspaceOut]:
    """Workspaces the caller can use. A token only ever sees its own workspace."""
    if principal.user_id is None:
        workspace = await workspace_by_id(session, principal.workspace_id)
        return [WorkspaceOut(id=workspace.id, name=workspace.name, role=principal.role)]
    return [
        WorkspaceOut(id=w.id, name=w.name, role=Role(m.role))
        for m, w in await memberships_of(session, principal.user_id)
    ]


@router.post("/workspaces", status_code=status.HTTP_201_CREATED)
async def new_workspace(
    body: CreateWorkspaceRequest, principal: CurrentPrincipal, session: Db
) -> WorkspaceOut:
    """Create a workspace owned by the signed-in person."""
    if principal.user_id is None:
        raise UserRequiredError
    user = await user_by_id(session, principal.user_id)
    workspace = await create_workspace(session, user, body.name)
    return WorkspaceOut(id=workspace.id, name=workspace.name, role=Role.OWNER)


@router.post("/workspaces/{workspace_id}/invites", status_code=status.HTTP_201_CREATED)
async def invite(
    workspace_id: str,
    body: InviteRequest,
    principal: Owner,
    session: Db,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> InviteOut:
    """Create a one-time invite link token for the caller's current workspace."""
    if workspace_id != principal.workspace_id:
        raise NotFoundError(workspace_id)
    row, token = await create_invite(
        scope_for(session, principal),
        principal,
        body.role,
        timedelta(hours=settings.invite_ttl_hours),
    )
    return InviteOut(invite_id=row.id, token=token, role=body.role, expires_at=row.expires_at)


@router.post("/invites/accept")
async def accept(body: AcceptInviteRequest, session: Db) -> AcceptInviteOut:
    """Join a workspace with an invite. New emails get an account; known ones must sign in."""
    user = await find_user(session, body.email)
    if user is None:
        user = await create_user(session, body.email, body.password)
    else:
        user = await authenticate(session, body.email, body.password)
    member = await accept_invite(session, user, body.token)
    return AcceptInviteOut(
        user_id=user.id, workspace_id=member.workspace_id, role=Role(member.role)
    )


@router.get("/members")
async def members(principal: Viewer, session: Db) -> list[MemberOut]:
    """Everyone in the current workspace and their role."""
    return [
        MemberOut(
            user_id=user.id, email=user.email, role=Role(member.role), joined_at=member.created_at
        )
        for member, user in await members_of(scope_for(session, principal))
    ]
