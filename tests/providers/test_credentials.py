from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.principal import Principal
from app.db import models
from app.db.scope import WorkspaceScope
from app.providers.credentials import (
    active_credential,
    create_credential,
    decrypt_credential,
    revoke_credential,
)
from app.providers.key_vault import LocalKeyVault
from tests.auth.helpers import owner_with_workspace

VAULT = LocalKeyVault(b"v" * 32)


async def _add(
    sessionmaker: async_sessionmaker[AsyncSession], owner: Principal, label: str, key: str
) -> models.ProviderCredential:
    # One transaction per key, so each one really is newer than the last.
    async with sessionmaker.begin() as session:
        return await create_credential(
            WorkspaceScope(session, owner.workspace_id),
            owner,
            models.KeyedProvider.GEMINI,
            label,
            key,
            VAULT,
        )


async def test_newest_active_key_is_used_and_revoking_removes_it(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker.begin() as session:
        _, owner = await owner_with_workspace(session)
    old = await _add(sessionmaker, owner, "old", "AIza-old-1111")
    new = await _add(sessionmaker, owner, "new", "AIza-new-2222")
    assert (old.key_last4, new.key_last4) == ("1111", "2222")
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, owner.workspace_id)
        active = await active_credential(scope, "gemini")
        assert active is not None
        assert decrypt_credential(active, VAULT) == "AIza-new-2222"
        await revoke_credential(scope, owner, new.id)
        fallback = await active_credential(scope, "gemini")
        assert fallback is not None and fallback.id == old.id
        await revoke_credential(scope, owner, old.id)
        assert await active_credential(scope, "gemini") is None
