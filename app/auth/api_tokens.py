from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.errors import RoleTooHighError
from app.auth.principal import Principal, role_at_least
from app.auth.tokens import hash_secret, new_secret
from app.db import models
from app.db.audit import record_audit
from app.db.ids import new_id
from app.db.scope import WorkspaceScope

TOKEN_PREFIX = "aet"  # noqa: S105


async def create_api_token(
    scope: WorkspaceScope, principal: Principal, name: str, role: models.Role
) -> tuple[models.ApiToken, str]:
    """Create a token. Its secret is returned once and only its hash is kept."""
    if not role_at_least(principal.role, role):
        raise RoleTooHighError
    secret = new_secret(TOKEN_PREFIX)
    token = models.ApiToken(
        id=new_id("tok"),
        workspace_id=scope.workspace_id,
        name=name,
        role=role,
        token_hash=hash_secret(secret),
        token_prefix=secret[: len(TOKEN_PREFIX) + 7],
        created_by_user_id=principal.user_id,
    )
    scope.add(token)
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "api_token.created",
        "api_token",
        token.id,
        {"role": role, "name": name},
    )
    await scope.flush()
    return token, secret


async def list_api_tokens(scope: WorkspaceScope) -> list[models.ApiToken]:
    return await scope.all(scope.query(models.ApiToken).order_by(models.ApiToken.created_at))


async def revoke_api_token(scope: WorkspaceScope, principal: Principal, token_id: str) -> None:
    token = await scope.require(models.ApiToken, token_id)
    if token.revoked_at is None:
        token.revoked_at = datetime.now(UTC)
        record_audit(
            scope,
            principal.actor_type,
            principal.actor_id,
            "api_token.revoked",
            "api_token",
            token.id,
        )


async def resolve_api_token(session: AsyncSession, secret: str) -> Principal | None:
    token = await session.scalar(
        select(models.ApiToken).where(models.ApiToken.token_hash == hash_secret(secret))
    )
    if token is None or token.revoked_at is not None:
        return None
    token.last_used_at = datetime.now(UTC)
    return Principal(
        actor_type=models.ActorType.TOKEN,
        actor_id=token.id,
        user_id=None,
        workspace_id=token.workspace_id,
        role=models.Role(token.role),
    )
