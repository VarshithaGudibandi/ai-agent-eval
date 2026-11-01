from datetime import datetime

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.api.dependencies import Db, Owner, scope_for
from app.auth.api_tokens import create_api_token, list_api_tokens, revoke_api_token
from app.db import models
from app.db.models import Role

router = APIRouter(prefix="/api/v1/api-tokens", tags=["api tokens"])


class TokenOut(BaseModel):
    id: str
    name: str
    role: Role
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked: bool

    @classmethod
    def of(cls, row: models.ApiToken) -> "TokenOut":
        return cls(
            id=row.id,
            name=row.name,
            role=Role(row.role),
            prefix=row.token_prefix,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            revoked=row.revoked_at is not None,
        )


class NewTokenRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role: Role


class NewTokenOut(TokenOut):
    token: str


@router.get("")
async def list_tokens(principal: Owner, session: Db) -> list[TokenOut]:
    """Tokens in this workspace. Secrets are never shown again after creation."""
    return [TokenOut.of(t) for t in await list_api_tokens(scope_for(session, principal))]


@router.post("", status_code=status.HTTP_201_CREATED)
async def new_token(body: NewTokenRequest, principal: Owner, session: Db) -> NewTokenOut:
    """Create a token. The secret is in this response only."""
    row, secret = await create_api_token(
        scope_for(session, principal), principal, body.name, body.role
    )
    await session.refresh(row)
    return NewTokenOut(**TokenOut.of(row).model_dump(), token=secret)


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke(token_id: str, principal: Owner, session: Db) -> None:
    """Revoke a token. It stops working at once."""
    await revoke_api_token(scope_for(session, principal), principal, token_id)
