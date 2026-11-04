from datetime import datetime

from fastapi import APIRouter, status
from pydantic import BaseModel

from app.api.dependencies import Db, Owner, Vault, Viewer, scope_for
from app.auth.signing_secrets import rotate_signing_secret, signing_secret_info
from app.db.audit import record_audit

router = APIRouter(prefix="/api/v1/agent-signing-secret", tags=["external agents"])


class SigningSecretOut(BaseModel):
    configured: bool
    last4: str | None
    created_at: datetime | None


class NewSigningSecretOut(SigningSecretOut):
    secret: str


@router.get("")
async def info(principal: Viewer, session: Db) -> SigningSecretOut:
    """Whether the workspace has a secret for signing agent requests. Never the secret."""
    found = await signing_secret_info(session, principal.workspace_id)
    return SigningSecretOut(**found.__dict__)


@router.post("", status_code=status.HTTP_201_CREATED)
async def rotate(principal: Owner, session: Db, vault: Vault) -> NewSigningSecretOut:
    """Create or replace the secret. It is shown this once, so the team can store it."""
    secret = await rotate_signing_secret(session, principal.workspace_id, vault)
    record_audit(
        scope_for(session, principal),
        principal.actor_type,
        principal.actor_id,
        "signing_secret.rotated",
        "workspace",
        principal.workspace_id,
    )
    await session.flush()
    found = await signing_secret_info(session, principal.workspace_id)
    return NewSigningSecretOut(secret=secret, **found.__dict__)
