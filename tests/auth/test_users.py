import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.errors import InvalidCredentialsError, SetupAlreadyDoneError
from app.auth.users import authenticate, create_user, setup_first_owner
from app.auth.workspaces import memberships_of
from app.db.ids import new_id


async def test_first_user_becomes_owner_and_setup_runs_once(
    fresh_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with fresh_sessionmaker.begin() as session:
        user, workspace = await setup_first_owner(
            session, " Owner@Example.com ", "a long password", "Team"
        )
        [(member, joined)] = await memberships_of(session, user.id)
    assert user.email == "owner@example.com"
    assert (member.role, joined.id) == ("owner", workspace.id)
    with pytest.raises(SetupAlreadyDoneError):
        async with fresh_sessionmaker.begin() as session:
            await setup_first_owner(session, "second@example.com", "a long password", "T2")


async def test_authenticate_checks_email_and_password(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    email = f"{new_id('u')}@example.com"
    async with sessionmaker.begin() as session:
        await create_user(session, email, "a long password")
    async with sessionmaker() as session:
        assert (await authenticate(session, email.upper(), "a long password")).email == email
        with pytest.raises(InvalidCredentialsError):
            await authenticate(session, email, "wrong password!")
        with pytest.raises(InvalidCredentialsError):
            await authenticate(session, "nobody@example.com", "a long password")
