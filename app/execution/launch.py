from collections.abc import Sequence
from typing import Any

from app.agents.tool_loop import ToolLoopSettings
from app.auth.signing_secrets import SigningSecretRequiredError, signing_secret_info
from app.budget.pricing import Price, latest_price
from app.db import models
from app.db.budgets import BudgetLimits
from app.db.scope import NotFoundError, WorkspaceScope
from app.providers.credentials import active_credential
from app.providers.registry import ProviderRegistry


class ProviderKeyRequiredError(Exception):
    def __init__(self, provider: str) -> None:
        super().__init__(f"add a {provider} key before launching a real experiment")
        self.provider = provider


class PriceUnknownError(ValueError):
    pass


async def launch_prices(
    scope: WorkspaceScope,
    agent_config_ids: Sequence[str],
    providers: ProviderRegistry,
    budget: BudgetLimits,
    mode: models.ExperimentMode = models.ExperimentMode.REAL,
) -> dict[str, dict[str, Any] | None]:
    """Check every model config can run, and freeze the price each one will be charged at."""
    if mode == models.ExperimentMode.DEMO:
        # Demo experiments run scripted agents only, which need no key and cost nothing.
        return {}
    configs = await scope.all(
        scope.query(models.AgentConfig).where(models.AgentConfig.id.in_(agent_config_ids))
    )
    if len(configs) != len(set(agent_config_ids)):
        raise NotFoundError("agent_configs")
    prices: dict[str, dict[str, Any] | None] = {}
    for config in configs:
        if config.adapter_type == models.AdapterType.HTTP:
            # Requests to an external agent are signed. Without a secret none can be sent.
            if not (await signing_secret_info(scope.session, scope.workspace_id)).configured:
                raise SigningSecretRequiredError()
            continue
        if config.adapter_type != models.AdapterType.TOOL_LOOP:
            continue
        settings = ToolLoopSettings.model_validate(config.settings)
        if providers.get(settings.provider).needs_key and (
            await active_credential(scope, settings.provider) is None
        ):
            raise ProviderKeyRequiredError(settings.provider)
        row = await latest_price(scope, settings.provider, settings.model)
        if row is None and budget.money_micros is not None:
            raise PriceUnknownError(
                f"no price for {settings.provider}/{settings.model}, so a money cap cannot work"
            )
        if row is not None and row.currency != budget.currency:
            raise PriceUnknownError(
                f"price is in {row.currency} but the budget is in {budget.currency}"
            )
        prices[config.id] = Price.of(row).model_dump() if row else None
    return prices
