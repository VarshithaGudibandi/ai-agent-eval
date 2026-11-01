from datetime import UTC, datetime

from app.auth.principal import Principal
from app.db import models
from app.db.audit import record_audit
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.providers.key_vault import KeyVault


def _context(workspace_id: str, credential_id: str, provider: str) -> str:
    # Binding the ciphertext to its row means a key copied into another workspace or
    # another row cannot be decrypted there.
    return f"{workspace_id}:{credential_id}:{provider}"


async def create_credential(
    scope: WorkspaceScope,
    principal: Principal,
    provider: models.KeyedProvider,
    label: str,
    api_key: str,
    vault: KeyVault,
) -> models.ProviderCredential:
    credential_id = new_id("cred")
    nonce, ciphertext = vault.encrypt(
        api_key, _context(scope.workspace_id, credential_id, provider)
    )
    row = models.ProviderCredential(
        id=credential_id,
        workspace_id=scope.workspace_id,
        provider=provider,
        label=label,
        nonce=nonce,
        ciphertext=ciphertext,
        key_last4=api_key[-4:],
        created_by_user_id=principal.user_id,
    )
    scope.add(row)
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "key.created",
        "provider_credential",
        row.id,
        {"provider": provider, "label": label},
    )
    await scope.flush()
    return row


async def list_credentials(scope: WorkspaceScope) -> list[models.ProviderCredential]:
    return await scope.all(
        scope.query(models.ProviderCredential).order_by(models.ProviderCredential.created_at)
    )


async def revoke_credential(
    scope: WorkspaceScope, principal: Principal, credential_id: str
) -> None:
    row = await scope.require(models.ProviderCredential, credential_id)
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        record_audit(
            scope,
            principal.actor_type,
            principal.actor_id,
            "key.revoked",
            "provider_credential",
            row.id,
        )


async def active_credential(
    scope: WorkspaceScope, provider: str
) -> models.ProviderCredential | None:
    return await scope.first(
        scope.query(models.ProviderCredential)
        .where(
            models.ProviderCredential.provider == provider,
            models.ProviderCredential.revoked_at.is_(None),
        )
        .order_by(models.ProviderCredential.created_at.desc())
    )


def decrypt_credential(row: models.ProviderCredential, vault: KeyVault) -> str:
    return vault.decrypt(
        row.nonce, row.ciphertext, _context(row.workspace_id, row.id, row.provider)
    )
