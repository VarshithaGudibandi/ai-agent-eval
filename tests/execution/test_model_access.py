import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.scope import WorkspaceScope
from app.execution.launch import ProviderKeyRequiredError
from app.execution.model_access import AttemptModels
from app.execution.trace_recorder import TraceRecorder
from app.providers.credentials import create_credential
from app.providers.gemini import GeminiProvider
from app.providers.key_vault import key_vault_from_settings
from app.providers.registry import default_providers
from app.settings import Settings
from tests.auth.helpers import owner_with_workspace


def _models(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, workspace_id: str
) -> AttemptModels:
    return AttemptModels(
        sessionmaker=sessionmaker,
        providers=default_providers(),
        settings=settings,
        workspace_id=workspace_id,
        experiment_id="exp_x",
        budget_id="bud_x",
        attempt_id="att_x",
        price=None,
        recorder=TraceRecorder(sessionmaker, workspace_id, "att_x"),
    )


async def test_gemini_client_uses_the_decrypted_workspace_key(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    async with sessionmaker.begin() as session:
        _, owner = await owner_with_workspace(session)
        await create_credential(
            WorkspaceScope(session, owner.workspace_id),
            owner,
            models.KeyedProvider.GEMINI,
            "k",
            "AIza-real-looking-5555",
            key_vault_from_settings(settings),
            requests_per_minute=15,
        )
    async with _models(sessionmaker, settings, owner.workspace_id) as access:
        client = await access.client("gemini")
    provider = client._provider
    assert isinstance(provider, GeminiProvider)
    assert provider._api_key == "AIza-real-looking-5555"
    assert client._limiter is not None


async def test_missing_key_is_reported_and_keyless_providers_need_none(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, workspace_id: str
) -> None:
    async with _models(sessionmaker, settings, workspace_id) as access:
        with pytest.raises(ProviderKeyRequiredError):
            await access.client("gemini")
        ollama = await access.client("ollama")
    assert ollama._limiter is None
