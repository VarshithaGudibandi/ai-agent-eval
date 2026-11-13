from collections.abc import Sequence

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.budget.pricing import Price
from app.budget.reservations import BudgetExhaustedError, BudgetGuard, BudgetOwner
from app.db import models
from app.db.budgets import BudgetLimits, budget_for_experiment
from app.db.scope import WorkspaceScope
from app.providers.contract import (
    Fatal,
    GenerationSettings,
    Message,
    ModelResponse,
    ProviderError,
    RateLimited,
    Retryable,
    ToolCallRequest,
    Usage,
)
from app.providers.metered import MeteredModel
from tests.conftest import MakeExperiment
from tests.providers.scripted import ScriptedProvider

PRICE = Price(
    price_version_id="price_t",
    currency="USD",
    input_per_million_micros=1_000_000,
    cached_input_per_million_micros=0,
    output_per_million_micros=2_000_000,
    other_per_million_micros=2_000_000,
)
SETTINGS = GenerationSettings(model="m", max_output_tokens=100)
MESSAGES = [Message(role="user", content="hello")]
REPLY = ModelResponse(
    text="hi",
    tool_calls=[],
    usage=Usage(input_tokens=10, cached_input_tokens=0, output_tokens=5, other_tokens=0),
    resolved_model="m-001",
)


async def _model(
    sessionmaker: async_sessionmaker[AsyncSession],
    experiment: models.Experiment,
    replies: Sequence[ModelResponse | ProviderError],
    max_retries: int = 2,
) -> tuple[MeteredModel, ScriptedProvider, list[float]]:
    async with sessionmaker() as session:
        budget = await budget_for_experiment(
            WorkspaceScope(session, experiment.workspace_id), experiment.id
        )
    provider = ScriptedProvider(replies)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    model = MeteredModel(
        provider,
        "scripted",
        price=PRICE,
        guard=BudgetGuard(
            sessionmaker,
            experiment.workspace_id,
            BudgetOwner("experiment", experiment.id),
            budget.id,
        ),
        limiter=None,
        sessionmaker=sessionmaker,
        workspace_id=experiment.workspace_id,
        timeout_seconds=5,
        max_retries=max_retries,
        sleep=fake_sleep,
    )
    return model, provider, slept


async def _calls_and_budget(
    sessionmaker: async_sessionmaker[AsyncSession], experiment: models.Experiment
) -> tuple[list[models.ModelCall], models.Budget]:
    async with sessionmaker() as session:
        scope = WorkspaceScope(session, experiment.workspace_id)
        calls = list(
            await session.scalars(
                select(models.ModelCall)
                .where(models.ModelCall.workspace_id == experiment.workspace_id)
                .order_by(models.ModelCall.retry_number)
            )
        )
        return calls, await budget_for_experiment(scope, experiment.id)


async def test_success_is_recorded_and_charged_its_real_cost(
    sessionmaker: async_sessionmaker[AsyncSession], make_experiment: MakeExperiment
) -> None:
    experiment = await make_experiment(BudgetLimits())
    model, _, _ = await _model(sessionmaker, experiment, [REPLY])
    assert (await model.generate(MESSAGES, [], SETTINGS)).text == "hi"
    [call], budget = await _calls_and_budget(sessionmaker, experiment)
    # 10 input tokens at 1 USD and 5 output at 2 USD per million: 20 micros.
    assert (call.status, call.cost_micros, call.resolved_model) == ("ok", 20, "m-001")
    assert (call.input_tokens, call.output_tokens) == (10, 5)
    assert (budget.money_used_micros, budget.tokens_used, budget.requests_used) == (20, 15, 1)


async def test_provider_signatures_reach_the_caller(
    sessionmaker: async_sessionmaker[AsyncSession], make_experiment: MakeExperiment
) -> None:
    experiment = await make_experiment(BudgetLimits())
    call = ToolCallRequest(id="c1", name="get_order", arguments={}, signature="opaque-blob")
    reply = REPLY.model_copy(update={"tool_calls": [call]})
    model, _, _ = await _model(sessionmaker, experiment, [reply])
    assert (await model.generate(MESSAGES, [], SETTINGS)).tool_calls == [call]


async def test_rate_limit_is_retried_free_and_recorded(
    sessionmaker: async_sessionmaker[AsyncSession], make_experiment: MakeExperiment
) -> None:
    experiment = await make_experiment(BudgetLimits())
    model, provider, slept = await _model(sessionmaker, experiment, [RateLimited("slow", 7), REPLY])
    await model.generate(MESSAGES, [], SETTINGS)
    calls, budget = await _calls_and_budget(sessionmaker, experiment)
    assert [(c.status, c.cost_micros) for c in calls] == [("rate_limited", 0), ("ok", 20)]
    assert slept == [7]
    assert (budget.money_used_micros, budget.requests_used) == (20, 1)
    assert len(provider.calls) == 2


async def test_errors_without_usage_keep_the_reserved_estimate(
    sessionmaker: async_sessionmaker[AsyncSession], make_experiment: MakeExperiment
) -> None:
    experiment = await make_experiment(BudgetLimits())
    model, _, slept = await _model(
        sessionmaker, experiment, [Retryable("503"), Retryable("503"), Retryable("503")]
    )
    with pytest.raises(Retryable):
        await model.generate(MESSAGES, [], SETTINGS)
    calls, budget = await _calls_and_budget(sessionmaker, experiment)
    assert [c.status for c in calls] == ["retryable_error"] * 3
    assert all(c.cost_micros is None for c in calls)
    assert len(slept) == 2
    assert budget.money_used_micros > 0
    assert budget.requests_used == 3


async def test_fatal_errors_are_not_retried(
    sessionmaker: async_sessionmaker[AsyncSession], make_experiment: MakeExperiment
) -> None:
    experiment = await make_experiment(BudgetLimits())
    model, provider, _ = await _model(sessionmaker, experiment, [Fatal("bad request"), REPLY])
    with pytest.raises(Fatal):
        await model.generate(MESSAGES, [], SETTINGS)
    assert len(provider.calls) == 1


async def test_no_call_is_made_without_budget(
    sessionmaker: async_sessionmaker[AsyncSession], make_experiment: MakeExperiment
) -> None:
    experiment = await make_experiment(BudgetLimits(money_micros=5))
    model, provider, _ = await _model(sessionmaker, experiment, [REPLY])
    with pytest.raises(BudgetExhaustedError):
        await model.generate(MESSAGES, [], SETTINGS)
    assert provider.calls == []
