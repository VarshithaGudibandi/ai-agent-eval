from collections.abc import Sequence

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.budget.pricing import Price
from app.budget.reservations import BudgetExhaustedError, BudgetGuard
from app.db import models
from app.db.budgets import budget_for_experiment
from app.db.scope import WorkspaceScope
from app.execution.trace_recorder import TraceRecorder
from app.providers.contract import (
    Fatal,
    GenerationSettings,
    Message,
    ModelResponse,
    ProviderError,
    RateLimited,
    Retryable,
    Usage,
)
from app.providers.metered import MeteredModel
from app.providers.scripted import ScriptedProvider

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
    attempt: models.Attempt,
    replies: Sequence[ModelResponse | ProviderError],
    max_retries: int = 2,
) -> tuple[MeteredModel, ScriptedProvider, list[float]]:
    async with sessionmaker() as session:
        run = await WorkspaceScope(session, attempt.workspace_id).require(
            models.Run, attempt.run_id
        )
        budget = await budget_for_experiment(
            WorkspaceScope(session, attempt.workspace_id), run.experiment_id
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
            sessionmaker, attempt.workspace_id, run.experiment_id, budget.id, attempt.id
        ),
        limiter=None,
        recorder=TraceRecorder(sessionmaker, attempt.workspace_id, attempt.id),
        sessionmaker=sessionmaker,
        workspace_id=attempt.workspace_id,
        attempt_id=attempt.id,
        timeout_seconds=5,
        max_retries=max_retries,
        sleep=fake_sleep,
    )
    return model, provider, slept


async def _calls_and_budget(
    sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt
) -> tuple[list[models.ModelCall], models.Budget, list[models.TraceEvent]]:
    async with sessionmaker() as session:
        scope = WorkspaceScope(session, attempt.workspace_id)
        calls = list(
            await session.scalars(
                select(models.ModelCall)
                .where(models.ModelCall.attempt_id == attempt.id)
                .order_by(models.ModelCall.retry_number)
            )
        )
        run = await scope.require(models.Run, attempt.run_id)
        budget = await budget_for_experiment(scope, run.experiment_id)
        events = list(
            await session.scalars(
                select(models.TraceEvent).where(models.TraceEvent.attempt_id == attempt.id)
            )
        )
        return calls, budget, events


async def test_success_is_traced_and_charged_its_real_cost(
    sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt
) -> None:
    model, _, _ = await _model(sessionmaker, attempt, [REPLY])
    assert (await model.generate(MESSAGES, [], SETTINGS)).text == "hi"
    [call], budget, [event] = await _calls_and_budget(sessionmaker, attempt)
    # 10 input tokens at 1 USD and 5 output at 2 USD per million: 20 micros.
    assert (call.status, call.cost_micros, call.resolved_model) == ("ok", 20, "m-001")
    assert (budget.money_used_micros, budget.tokens_used, budget.requests_used) == (20, 15, 1)
    assert event.event_type == "model_call"
    assert event.id == call.trace_event_id
    assert event.payload["usage"]["output_tokens"] == 5


async def test_rate_limit_is_retried_free_and_recorded(
    sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt
) -> None:
    model, provider, slept = await _model(sessionmaker, attempt, [RateLimited("slow", 7), REPLY])
    await model.generate(MESSAGES, [], SETTINGS)
    calls, budget, _ = await _calls_and_budget(sessionmaker, attempt)
    assert [(c.status, c.cost_micros) for c in calls] == [("rate_limited", 0), ("ok", 20)]
    assert slept == [7]
    assert (budget.money_used_micros, budget.requests_used) == (20, 1)
    assert len(provider.calls) == 2


async def test_errors_without_usage_keep_the_reserved_estimate(
    sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt
) -> None:
    model, _, slept = await _model(
        sessionmaker, attempt, [Retryable("503"), Retryable("503"), Retryable("503")]
    )
    with pytest.raises(Retryable):
        await model.generate(MESSAGES, [], SETTINGS)
    calls, budget, _ = await _calls_and_budget(sessionmaker, attempt)
    assert [c.status for c in calls] == ["retryable_error"] * 3
    assert all(c.cost_micros is None for c in calls)
    assert len(slept) == 2
    assert budget.money_used_micros > 0
    assert budget.requests_used == 3


async def test_fatal_errors_are_not_retried(
    sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt
) -> None:
    model, provider, _ = await _model(sessionmaker, attempt, [Fatal("bad request"), REPLY])
    with pytest.raises(Fatal):
        await model.generate(MESSAGES, [], SETTINGS)
    assert len(provider.calls) == 1


async def test_no_call_is_made_without_budget(
    sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt
) -> None:
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, attempt.workspace_id)
        run = await scope.require(models.Run, attempt.run_id)
        (await budget_for_experiment(scope, run.experiment_id)).money_limit_micros = 5
        (await scope.require(models.Experiment, run.experiment_id)).status = "running"
    model, provider, _ = await _model(sessionmaker, attempt, [REPLY])
    with pytest.raises(BudgetExhaustedError):
        await model.generate(MESSAGES, [], SETTINGS)
    assert provider.calls == []
