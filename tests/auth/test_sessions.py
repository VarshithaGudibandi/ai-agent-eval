from datetime import timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.errors import NotAMemberError
from app.auth.sessions import csrf_valid, end_session, resolve_session, start_session
from app.db import models
from tests.auth.helpers import owner_with_workspace

HOUR = timedelta(hours=1)


async def test_session_resolves_to_its_member_and_checks_csrf(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker.begin() as session:
        user, owner = await owner_with_workspace(session)
        new = await start_session(session, user, None, HOUR)
    async with sessionmaker() as session:
        resolved = await resolve_session(session, new.token)
    assert resolved is not None
    principal, row = resolved
    assert (principal.workspace_id, principal.role) == (owner.workspace_id, "owner")
    assert csrf_valid(row, new.csrf_token)
    assert not csrf_valid(row, None)
    assert not csrf_valid(row, new.token)


async def test_ended_expired_and_unknown_sessions_do_not_resolve(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker.begin() as session:
        user, _ = await owner_with_workspace(session)
        ended = await start_session(session, user, None, HOUR)
        expired = await start_session(session, user, None, -HOUR)
        await end_session(session, ended.row.id)
    async with sessionmaker() as session:
        assert await resolve_session(session, ended.token) is None
        assert await resolve_session(session, expired.token) is None
        assert await resolve_session(session, "ses_made_up") is None


async def test_removing_a_member_ends_their_sessions(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker.begin() as session:
        user, owner = await owner_with_workspace(session)
        new = await start_session(session, user, None, HOUR)
        await session.execute(
            delete(models.Membership).where(models.Membership.workspace_id == owner.workspace_id)
        )
    async with sessionmaker() as session:
        assert await resolve_session(session, new.token) is None


async def test_cannot_start_a_session_in_a_workspace_you_do_not_belong_to(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        user, _ = await owner_with_workspace(session)
        with pytest.raises(NotAMemberError):
            await start_session(session, user, workspace_id, HOUR)
