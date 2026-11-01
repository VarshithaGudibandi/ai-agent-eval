from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.errors import InviteInvalidError, UserRequiredError
from app.auth.users import create_user
from app.auth.workspaces import accept_invite, create_invite, memberships_of
from app.db import models
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from tests.auth.helpers import owner_with_workspace

DAY = timedelta(days=1)


async def test_invite_link_adds_a_member_once(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker.begin() as session:
        _, owner = await owner_with_workspace(session)
        scope = WorkspaceScope(session, owner.workspace_id)
        _, secret = await create_invite(scope, owner, models.Role.VIEWER, DAY)
        guest = await create_user(session, f"{new_id('g')}@example.com", "a long password")
        member = await accept_invite(session, guest, secret)
        assert (member.workspace_id, member.role) == (owner.workspace_id, "viewer")
        other = await create_user(session, f"{new_id('g')}@example.com", "a long password")
        with pytest.raises(InviteInvalidError):
            await accept_invite(session, other, secret)
        assert [m.workspace_id for m, _ in await memberships_of(session, guest.id)] == [
            owner.workspace_id
        ]
        actions = {e.action for e in await scope.all(scope.query(models.AuditEntry))}
        assert actions == {"workspace.created", "member.invited", "member.joined"}


async def test_expired_and_unknown_invites_are_refused(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker.begin() as session:
        _, owner = await owner_with_workspace(session)
        _, secret = await create_invite(
            WorkspaceScope(session, owner.workspace_id), owner, models.Role.MEMBER, -DAY
        )
        guest = await create_user(session, f"{new_id('g')}@example.com", "a long password")
        for bad in (secret, "inv_made_up"):
            with pytest.raises(InviteInvalidError):
                await accept_invite(session, guest, bad)


async def test_tokens_cannot_invite(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker.begin() as session:
        _, owner = await owner_with_workspace(session)
        token_principal = owner.model_copy(
            update={"actor_type": models.ActorType.TOKEN, "user_id": None}
        )
        with pytest.raises(UserRequiredError):
            await create_invite(
                WorkspaceScope(session, owner.workspace_id),
                token_principal,
                models.Role.MEMBER,
                DAY,
            )
