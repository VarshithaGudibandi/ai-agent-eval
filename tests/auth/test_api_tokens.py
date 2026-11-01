import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.api_tokens import (
    create_api_token,
    list_api_tokens,
    resolve_api_token,
    revoke_api_token,
)
from app.auth.errors import RoleTooHighError
from app.db import models
from app.db.scope import NotFoundError, WorkspaceScope
from tests.auth.helpers import owner_with_workspace


async def test_token_works_until_revoked_and_stores_no_secret(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker.begin() as session:
        _, owner = await owner_with_workspace(session)
        scope = WorkspaceScope(session, owner.workspace_id)
        token, secret = await create_api_token(scope, owner, "ci", models.Role.MEMBER)
        assert secret not in (token.token_hash, token.token_prefix)
        assert secret.startswith(token.token_prefix)
    async with sessionmaker.begin() as session:
        principal = await resolve_api_token(session, secret)
        assert principal is not None
        assert (principal.workspace_id, principal.role, principal.user_id) == (
            owner.workspace_id,
            "member",
            None,
        )
        await revoke_api_token(WorkspaceScope(session, owner.workspace_id), owner, token.id)
    async with sessionmaker() as session:
        assert await resolve_api_token(session, secret) is None
        listed = await list_api_tokens(WorkspaceScope(session, owner.workspace_id))
        assert [t.revoked_at is not None for t in listed] == [True]


async def test_cannot_mint_a_token_above_your_own_role(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker.begin() as session:
        _, owner = await owner_with_workspace(session)
        member = owner.model_copy(update={"role": models.Role.MEMBER})
        with pytest.raises(RoleTooHighError):
            await create_api_token(
                WorkspaceScope(session, owner.workspace_id), member, "x", models.Role.OWNER
            )


async def test_cannot_revoke_another_workspaces_token(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker.begin() as session:
        _, a = await owner_with_workspace(session)
        _, b = await owner_with_workspace(session)
        token, _ = await create_api_token(
            WorkspaceScope(session, b.workspace_id), b, "t", models.Role.VIEWER
        )
        with pytest.raises(NotFoundError):
            await revoke_api_token(WorkspaceScope(session, a.workspace_id), a, token.id)
