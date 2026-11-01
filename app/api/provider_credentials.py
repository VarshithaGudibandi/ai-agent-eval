from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, SecretStr

from app.api.dependencies import Db, Owner, get_settings_dep, scope_for
from app.db import models
from app.providers.credentials import create_credential, list_credentials, revoke_credential
from app.providers.key_vault import KeyVault, MasterKeyMissingError, key_vault_from_settings
from app.settings import Settings

router = APIRouter(prefix="/api/v1/provider-credentials", tags=["provider keys"])

FREE_TIER_WARNING = {
    models.KeyedProvider.GEMINI: (
        "Google may use free-tier Gemini traffic to improve its products. "
        "Send only synthetic data through a free-tier key."
    ),
}


def key_vault(settings: Annotated[Settings, Depends(get_settings_dep)]) -> KeyVault:
    try:
        return key_vault_from_settings(settings)
    except MasterKeyMissingError as exc:
        raise HTTPException(
            status_code=503, detail={"error": "master_key_missing", "message": str(exc)}
        ) from None


class NewCredentialRequest(BaseModel):
    provider: models.KeyedProvider
    label: str = Field(min_length=1, max_length=200)
    api_key: SecretStr = Field(min_length=8, max_length=500)
    requests_per_minute: int | None = Field(default=None, gt=0)
    tokens_per_minute: int | None = Field(default=None, gt=0)


class CredentialOut(BaseModel):
    id: str
    provider: models.KeyedProvider
    label: str
    last4: str
    created_at: datetime
    revoked: bool
    requests_per_minute: int | None
    tokens_per_minute: int | None
    warning: str | None

    @classmethod
    def of(cls, row: models.ProviderCredential) -> "CredentialOut":
        provider = models.KeyedProvider(row.provider)
        return cls(
            id=row.id,
            provider=provider,
            label=row.label,
            last4=row.key_last4,
            created_at=row.created_at,
            revoked=row.revoked_at is not None,
            requests_per_minute=row.requests_per_minute,
            tokens_per_minute=row.tokens_per_minute,
            warning=FREE_TIER_WARNING.get(provider),
        )


@router.get("")
async def credentials(principal: Owner, session: Db) -> list[CredentialOut]:
    """Provider keys in this workspace. Only the label and last four characters are shown."""
    return [CredentialOut.of(c) for c in await list_credentials(scope_for(session, principal))]


@router.post("", status_code=status.HTTP_201_CREATED)
async def new_credential(
    body: NewCredentialRequest,
    principal: Owner,
    session: Db,
    vault: Annotated[KeyVault, Depends(key_vault)],
) -> CredentialOut:
    """Store a provider key, encrypted. It is never shown again.

    The per-minute limits feed a rate limiter shared by every worker using this key.
    """
    row = await create_credential(
        scope_for(session, principal),
        principal,
        body.provider,
        body.label,
        body.api_key.get_secret_value(),
        vault,
        requests_per_minute=body.requests_per_minute,
        tokens_per_minute=body.tokens_per_minute,
    )
    await session.refresh(row)
    return CredentialOut.of(row)


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke(credential_id: str, principal: Owner, session: Db) -> None:
    """Revoke a provider key. Experiments stop using it at once."""
    await revoke_credential(scope_for(session, principal), principal, credential_id)
