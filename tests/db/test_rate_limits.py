import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.rate_limits import take
from app.db.scope import WorkspaceScope
from app.providers.credentials import create_credential
from app.providers.key_vault import LocalKeyVault
from tests.auth.helpers import owner_with_workspace


async def credential_with_limits(
    sessionmaker: async_sessionmaker[AsyncSession], rpm: int | None, tpm: int | None
) -> tuple[str, str]:
    async with sessionmaker.begin() as session:
        _, owner = await owner_with_workspace(session)
        row = await create_credential(
            WorkspaceScope(session, owner.workspace_id),
            owner,
            models.KeyedProvider.GEMINI,
            "k",
            "AIza-key-0000",
            LocalKeyVault(b"r" * 32),
            requests_per_minute=rpm,
            tokens_per_minute=tpm,
        )
        return owner.workspace_id, row.id


async def _take(
    sessionmaker: async_sessionmaker[AsyncSession],
    workspace_id: str,
    credential_id: str,
    tokens: int,
) -> float:
    async with sessionmaker.begin() as session:
        return await take(WorkspaceScope(session, workspace_id), credential_id, tokens)


async def test_requests_per_minute_bucket(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    workspace_id, credential_id = await credential_with_limits(sessionmaker, rpm=2, tpm=None)
    waits = [await _take(sessionmaker, workspace_id, credential_id, 1) for _ in range(3)]
    assert waits[:2] == [0, 0]
    assert 25 < waits[2] <= 30


async def test_tokens_per_minute_bucket(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    workspace_id, credential_id = await credential_with_limits(sessionmaker, rpm=None, tpm=1_000)
    assert await _take(sessionmaker, workspace_id, credential_id, 800) == 0
    wait = await _take(sessionmaker, workspace_id, credential_id, 800)
    assert 30 < wait <= 36


async def test_no_limits_never_wait(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    workspace_id, credential_id = await credential_with_limits(sessionmaker, rpm=None, tpm=None)
    assert [await _take(sessionmaker, workspace_id, credential_id, 10**9) for _ in range(5)] == [
        0
    ] * 5


async def test_workers_share_one_bucket(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    workspace_id, credential_id = await credential_with_limits(sessionmaker, rpm=5, tpm=None)
    waits = await asyncio.gather(
        *(_take(sessionmaker, workspace_id, credential_id, 1) for _ in range(12))
    )
    assert sum(1 for w in waits if w == 0) == 5


async def test_another_workspace_cannot_use_the_bucket(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    _, credential_id = await credential_with_limits(sessionmaker, rpm=1, tpm=None)
    with pytest.raises(LookupError):
        await _take(sessionmaker, workspace_id, credential_id, 1)
