from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.contract import RunLimits
from app.budget.pricing import PriceInput, create_price_version
from app.db import models
from app.db.budgets import BudgetLimits
from app.db.content import create_agent_config, create_dataset_version, load_task_file
from app.db.experiments import create_experiment
from app.db.scope import WorkspaceScope
from app.environments.registry import default_registry
from app.evaluators.registry import default_evaluators
from app.execution.attempt_runner import Platform
from app.execution.launch import launch_prices
from app.providers.contract import ModelResponse, ProviderError, ToolCallRequest, Usage
from app.providers.registry import ProviderEntry, ProviderRegistry
from app.providers.scripted import ScriptedProvider
from app.settings import Settings
from tests.conftest import TASK_FILE

# 1 micro per input token and 2 per output token: each reply below costs 1,100 micros.
PRICE = PriceInput(
    input_per_million_micros=1_000_000,
    cached_input_per_million_micros=0,
    output_per_million_micros=2_000_000,
    other_per_million_micros=2_000_000,
)
USAGE = Usage(input_tokens=1_000, cached_input_tokens=0, output_tokens=50, other_tokens=0)
COST_PER_CALL = 1_100


def _reply(text: str, *calls: ToolCallRequest) -> ModelResponse:
    return ModelResponse(
        text=text, tool_calls=list(calls), usage=USAGE, resolved_model="scripted-1"
    )


def refund_conversation() -> list[ModelResponse | ProviderError]:
    """What a capable model would say on the refund task, one reply per turn."""
    return [
        _reply("", ToolCallRequest(id="c1", name="get_order", arguments={"order_id": "ord_1001"})),
        _reply(
            "",
            ToolCallRequest(
                id="c2",
                name="issue_refund",
                arguments={"order_id": "ord_1001", "amount_minor": 24999, "reason": "damaged"},
            ),
        ),
        _reply("I have refunded $249.99 to your card."),
    ]


Replies = Callable[[], list[ModelResponse | ProviderError]]


def scripted_providers(replies: Replies = refund_conversation) -> ProviderRegistry:
    # A fresh provider per attempt, so every attempt hears the whole conversation.
    return ProviderRegistry(
        {
            "scripted": ProviderEntry(
                needs_key=False, build=lambda key, s, http: ScriptedProvider(replies())
            )
        }
    )


@pytest.fixture
def platform_with(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> Callable[[ProviderRegistry], Platform]:
    def make(providers: ProviderRegistry) -> Platform:
        return Platform(
            sessionmaker,
            default_registry(),
            default_evaluators(),
            providers=providers,
            settings=settings,
        )

    return make


@dataclass
class ToolLoopExperiment:
    experiment_id: str
    config_id: str


MakeToolLoopExperiment = Callable[[BudgetLimits, int], Awaitable[ToolLoopExperiment]]


@pytest.fixture
def tool_loop_experiment(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> MakeToolLoopExperiment:
    async def make(budget: BudgetLimits, repetitions: int) -> ToolLoopExperiment:
        async with sessionmaker.begin() as session:
            scope = WorkspaceScope(session, workspace_id)
            await create_price_version(scope, "scripted", "m", PRICE)
            version = await create_dataset_version(scope, "retail", [load_task_file(TASK_FILE)])
            config = await create_agent_config(
                scope,
                "loop",
                models.AdapterType.TOOL_LOOP,
                {"provider": "scripted", "model": "m", "max_output_tokens": 200},
                RunLimits(),
            )
            prices = await launch_prices(scope, [config.id], scripted_providers(), budget)
            experiment = await create_experiment(
                scope,
                version.id,
                [config.id],
                default_evaluators(),
                mode=models.ExperimentMode.REAL,
                repetitions=repetitions,
                budget=budget,
                prices=prices,
            )
            return ToolLoopExperiment(experiment.id, config.id)

    return make
