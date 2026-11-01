from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.auth.tokens import hash_secret, new_secret, secrets_match
from app.auth.workspaces import membership
from app.db import models
from app.db.ids import new_id


@dataclass(frozen=True)
class NewSession:
    row: models.UserSession
    token: str
    csrf_token: str
    role: models.Role


async def start_session(
    session: AsyncSession, user: models.User, workspace_id: str | None, ttl: timedelta
) -> NewSession:
    member = await membership(session, user.id, workspace_id)
    token, csrf = new_secret("ses"), new_secret("csrf")
    row = models.UserSession(
        id=new_id("ses"),
        workspace_id=member.workspace_id,
        user_id=user.id,
        token_hash=hash_secret(token),
        csrf_hash=hash_secret(csrf),
        expires_at=datetime.now(UTC) + ttl,
    )
    session.add(row)
    await session.flush()
    return NewSession(row=row, token=token, csrf_token=csrf, role=models.Role(member.role))


async def resolve_session(
    session: AsyncSession, token: str
) -> tuple[Principal, models.UserSession] | None:
    found = (
        await session.execute(
            select(models.UserSession, models.Membership)
            .join(
                models.Membership,
                (models.Membership.workspace_id == models.UserSession.workspace_id)
                & (models.Membership.user_id == models.UserSession.user_id),
            )
            .where(models.UserSession.token_hash == hash_secret(token))
        )
    ).one_or_none()
    if found is None:
        return None
    row, member = found
    if row.revoked_at is not None or row.expires_at <= datetime.now(UTC):
        return None
    # The role is read from the membership on every request, so a role change or a
    # removal takes effect at once instead of when the session expires.
    principal = Principal(
        actor_type=models.ActorType.USER,
        actor_id=row.user_id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        role=models.Role(member.role),
        session_id=row.id,
    )
    return principal, row


def csrf_valid(row: models.UserSession, csrf_token: str | None) -> bool:
    return csrf_token is not None and secrets_match(csrf_token, row.csrf_hash)


async def end_session(session: AsyncSession, session_id: str) -> None:
    row = await session.get(models.UserSession, session_id)
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
