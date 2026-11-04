from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.errors import InviteInvalidError, NotAMemberError, UserRequiredError
from app.auth.principal import Principal
from app.auth.tokens import hash_secret, new_secret
from app.db import models
from app.db.audit import record_audit
from app.db.ids import new_id
from app.db.scope import WorkspaceScope


async def create_workspace(
    session: AsyncSession, owner: models.User, name: str
) -> models.Workspace:
    workspace = models.Workspace(id=new_id("ws"), name=name)
    session.add(workspace)
    await session.flush()
    scope = WorkspaceScope(session, workspace.id)
    scope.add(
        models.Membership(workspace_id=workspace.id, user_id=owner.id, role=models.Role.OWNER)
    )
    record_audit(
        scope, models.ActorType.USER, owner.id, "workspace.created", "workspace", workspace.id
    )
    await session.flush()
    return workspace


async def memberships_of(
    session: AsyncSession, user_id: str
) -> list[tuple[models.Membership, models.Workspace]]:
    rows = await session.execute(
        select(models.Membership, models.Workspace)
        .join(models.Workspace, models.Workspace.id == models.Membership.workspace_id)
        .where(models.Membership.user_id == user_id)
        .order_by(models.Membership.created_at, models.Workspace.id)
    )
    return [(m, w) for m, w in rows]


async def membership(
    session: AsyncSession, user_id: str, workspace_id: str | None
) -> models.Membership:
    """The user's membership in the given workspace, or their first one if none is given."""
    for member, workspace in await memberships_of(session, user_id):
        if workspace_id is None or workspace.id == workspace_id:
            return member
    raise NotAMemberError


async def create_invite(
    scope: WorkspaceScope, principal: Principal, role: models.Role, ttl: timedelta
) -> tuple[models.Invite, str]:
    if principal.user_id is None:
        raise UserRequiredError
    secret = new_secret("inv")
    invite = models.Invite(
        id=new_id("inv"),
        workspace_id=scope.workspace_id,
        role=role,
        token_hash=hash_secret(secret),
        created_by_user_id=principal.user_id,
        expires_at=datetime.now(UTC) + ttl,
    )
    scope.add(invite)
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "member.invited",
        "invite",
        invite.id,
        {"role": role},
    )
    await scope.flush()
    return invite, secret


async def accept_invite(session: AsyncSession, user: models.User, secret: str) -> models.Membership:
    invite = await session.scalar(
        select(models.Invite)
        .where(models.Invite.token_hash == hash_secret(secret))
        .with_for_update()
    )
    now = datetime.now(UTC)
    if invite is None or invite.accepted_at is not None or invite.expires_at <= now:
        raise InviteInvalidError
    scope = WorkspaceScope(session, invite.workspace_id)
    existing = await scope.first(
        scope.query(models.Membership).where(models.Membership.user_id == user.id)
    )
    if existing is not None:
        raise InviteInvalidError
    member = models.Membership(workspace_id=invite.workspace_id, user_id=user.id, role=invite.role)
    scope.add(member)
    invite.accepted_at = now
    invite.accepted_by_user_id = user.id
    record_audit(
        scope,
        models.ActorType.USER,
        user.id,
        "member.joined",
        "membership",
        user.id,
        {"role": invite.role, "invite_id": invite.id},
    )
    await session.flush()
    return member


async def workspace_by_id(session: AsyncSession, workspace_id: str) -> models.Workspace:
    return await session.get_one(models.Workspace, workspace_id)


async def members_of(
    scope: WorkspaceScope,
) -> list[tuple[models.Membership, models.User]]:
    """Everyone in the workspace with their role, earliest first."""
    rows = await scope.session.execute(
        select(models.Membership, models.User)
        .join(models.User, models.User.id == models.Membership.user_id)
        .where(models.Membership.workspace_id == scope.workspace_id)
        .order_by(models.Membership.created_at, models.User.email)
    )
    return [(m, u) for m, u in rows]
