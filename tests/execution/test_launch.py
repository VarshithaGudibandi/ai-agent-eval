import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.contract import RunLimits
from app.budget.pricing import create_price_version
from app.db import models
from app.db.budgets import BudgetLimits
from app.db.content import create_agent_config
from app.db.scope import NotFoundError, WorkspaceScope
from app.execution.launch import PriceUnknownError, ProviderKeyRequiredError, launch_prices
from app.providers.credentials import create_credential
from app.providers.key_vault import LocalKeyVault
from app.providers.registry import default_providers
from tests.auth.helpers import owner_with_workspace
from tests.execution.conftest import PRICE


async def _config(scope: WorkspaceScope, provider: str, model: str = "m") -> str:
    row = await create_agent_config(
        scope,
        "c",
        models.AdapterType.TOOL_LOOP,
        {"provider": provider, "model": model},
        RunLimits(),
    )
    return row.id


async def test_gemini_needs_a_key_and_a_key_unblocks_it(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker.begin() as session:
        _, owner = await owner_with_workspace(session)
        scope = WorkspaceScope(session, owner.workspace_id)
        config_id = await _config(scope, "gemini")
        with pytest.raises(ProviderKeyRequiredError):
            await launch_prices(scope, [config_id], default_providers(), BudgetLimits())
        await create_credential(
            scope,
            owner,
            models.KeyedProvider.GEMINI,
            "k",
            "AIza-key-1234",
            LocalKeyVault(b"l" * 32),
        )
        assert await launch_prices(scope, [config_id], default_providers(), BudgetLimits()) == {
            config_id: None
        }


async def test_money_cap_needs_a_price_in_the_same_currency(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        config_id = await _config(scope, "ollama", "llama")
        capped = BudgetLimits(money_micros=1_000)
        with pytest.raises(PriceUnknownError):
            await launch_prices(scope, [config_id], default_providers(), capped)
        await create_price_version(scope, "ollama", "llama", PRICE)
        frozen = await launch_prices(scope, [config_id], default_providers(), capped)
        assert frozen[config_id] is not None
        assert frozen[config_id]["output_per_million_micros"] == 2_000_000
        with pytest.raises(PriceUnknownError):
            await launch_prices(
                scope,
                [config_id],
                default_providers(),
                BudgetLimits(currency="EUR", money_micros=1),
            )


async def test_unknown_configs_are_not_found(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with sessionmaker() as session:
        with pytest.raises(NotFoundError):
            await launch_prices(
                WorkspaceScope(session, workspace_id),
                ["cfg_nope"],
                default_providers(),
                BudgetLimits(),
            )
