import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.providers.key_vault import KeyVault


class SigningSecretRequiredError(Exception):
    def __init__(self) -> None:
        super().__init__("create the workspace's agent signing secret before running HTTP agents")


@dataclass(frozen=True)
class SigningSecretInfo:
    configured: bool
    last4: str | None
    created_at: datetime | None


def _context(workspace_id: str) -> str:
    # Binding the ciphertext to its workspace stops a row being copied to another one.
    return f"signing-secret:{workspace_id}"


async def rotate_signing_secret(session: AsyncSession, workspace_id: str, vault: KeyVault) -> str:
    """Make a new secret, replacing any old one. The caller shows it once and never again."""
    workspace = await session.get_one(models.Workspace, workspace_id)
    secret = f"whsec_{secrets.token_urlsafe(32)}"
    nonce, ciphertext = vault.encrypt(secret, _context(workspace_id))
    workspace.signing_secret_nonce = nonce
    workspace.signing_secret_ciphertext = ciphertext
    workspace.signing_secret_last4 = secret[-4:]
    workspace.signing_secret_created_at = datetime.now(UTC)
    return secret


async def signing_secret(session: AsyncSession, workspace_id: str, vault: KeyVault) -> str:
    workspace = await session.get_one(models.Workspace, workspace_id)
    if workspace.signing_secret_nonce is None or workspace.signing_secret_ciphertext is None:
        raise SigningSecretRequiredError()
    return vault.decrypt(
        workspace.signing_secret_nonce, workspace.signing_secret_ciphertext, _context(workspace_id)
    )


async def signing_secret_info(session: AsyncSession, workspace_id: str) -> SigningSecretInfo:
    workspace = await session.get_one(models.Workspace, workspace_id)
    return SigningSecretInfo(
        configured=workspace.signing_secret_ciphertext is not None,
        last4=workspace.signing_secret_last4,
        created_at=workspace.signing_secret_created_at,
    )
