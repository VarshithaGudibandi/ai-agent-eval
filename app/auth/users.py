from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.errors import InvalidCredentialsError, SetupAlreadyDoneError
from app.auth.passwords import hash_password, verify_password
from app.auth.workspaces import create_workspace
from app.db import models
from app.db.ids import new_id

# Any fixed number works; it only has to be the same for every process.
_SETUP_LOCK = 4_210_771


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def find_user(session: AsyncSession, email: str) -> models.User | None:
    return await session.scalar(
        select(models.User).where(models.User.email == normalize_email(email))
    )


async def create_user(session: AsyncSession, email: str, password: str) -> models.User:
    user = models.User(
        id=new_id("usr"), email=normalize_email(email), password_hash=hash_password(password)
    )
    session.add(user)
    await session.flush()
    return user


async def setup_first_owner(
    session: AsyncSession, email: str, password: str, workspace_name: str
) -> tuple[models.User, models.Workspace]:
    """Make the very first user the owner of the first workspace. Works only once."""
    # The lock makes two people racing to set up a fresh install take turns.
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SETUP_LOCK})
    if await session.scalar(select(func.count()).select_from(models.User)):
        raise SetupAlreadyDoneError
    user = await create_user(session, email, password)
    workspace = await create_workspace(session, user, workspace_name)
    return user, workspace


async def authenticate(session: AsyncSession, email: str, password: str) -> models.User:
    user = await find_user(session, email)
    if not verify_password(user.password_hash if user else None, password) or user is None:
        raise InvalidCredentialsError
    return user


async def user_by_id(session: AsyncSession, user_id: str) -> models.User:
    return await session.get_one(models.User, user_id)
