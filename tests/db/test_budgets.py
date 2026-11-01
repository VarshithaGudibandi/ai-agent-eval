import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.budgets import (
    BudgetLimits,
    LimitBelowUsageError,
    budget_for_experiment,
    raise_limits,
    settle,
    try_reserve,
)
from app.db.scope import WorkspaceScope
from tests.conftest import MakeExperiment


async def _budget(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, experiment_id: str
) -> models.Budget:
    async with sessionmaker() as session:
        return await budget_for_experiment(WorkspaceScope(session, workspace_id), experiment_id)


async def _reserve(
    sessionmaker: async_sessionmaker[AsyncSession],
    workspace_id: str,
    budget_id: str,
    **amounts: int,
) -> bool:
    async with sessionmaker.begin() as session:
        entry = await try_reserve(
            WorkspaceScope(session, workspace_id), budget_id, attempt_id=None, **amounts
        )
        return entry is not None


@pytest.mark.parametrize(
    ("limits", "amounts", "fits"),
    [
        (BudgetLimits(money_micros=1_000), {"money_micros": 100, "tokens": 1, "requests": 1}, 10),
        (BudgetLimits(tokens=1_050), {"money_micros": 0, "tokens": 100, "requests": 1}, 10),
        (BudgetLimits(requests=7), {"money_micros": 0, "tokens": 0, "requests": 1}, 7),
    ],
)
async def test_twenty_concurrent_reservations_never_overspend(
    sessionmaker: async_sessionmaker[AsyncSession],
    workspace_id: str,
    make_experiment: MakeExperiment,
    limits: BudgetLimits,
    amounts: dict[str, int],
    fits: int,
) -> None:
    experiment = await make_experiment(limits)
    budget = await _budget(sessionmaker, workspace_id, experiment.id)
    results = await asyncio.gather(
        *(_reserve(sessionmaker, workspace_id, budget.id, **amounts) for _ in range(20))
    )
    assert sum(results) == fits
    after = await _budget(sessionmaker, workspace_id, experiment.id)
    assert after.money_used_micros == fits * amounts["money_micros"]
    assert after.tokens_used == fits * amounts["tokens"]
    assert after.requests_used == fits * amounts["requests"]
    for used, limit in [
        (after.money_used_micros, after.money_limit_micros),
        (after.tokens_used, after.tokens_limit),
        (after.requests_used, after.requests_limit),
    ]:
        assert limit is None or used <= limit
    async with sessionmaker() as session:
        entries = await session.scalar(
            select(func.count()).where(models.SpendEntry.budget_id == budget.id)
        )
    assert entries == fits


async def test_settling_releases_unused_reservation_and_keeps_unknown_cost(
    sessionmaker: async_sessionmaker[AsyncSession],
    workspace_id: str,
    make_experiment: MakeExperiment,
) -> None:
    experiment = await make_experiment(BudgetLimits(money_micros=1_000))
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        budget = await budget_for_experiment(scope, experiment.id)
        known = await try_reserve(
            scope, budget.id, money_micros=400, tokens=50, requests=1, attempt_id=None
        )
        unknown = await try_reserve(
            scope, budget.id, money_micros=300, tokens=40, requests=1, attempt_id=None
        )
        assert known is not None and unknown is not None
        await settle(scope, known, money_micros=120, tokens=20, requests=1)
        await settle(scope, unknown, money_micros=None, tokens=None, requests=1)
        await session.refresh(budget)
        assert (budget.money_used_micros, budget.tokens_used, budget.requests_used) == (420, 60, 2)
        ledger_money = await session.scalar(
            select(func.sum(models.SpendEntry.money_micros)).where(
                models.SpendEntry.budget_id == budget.id
            )
        )
        assert ledger_money == budget.money_used_micros


async def test_limits_can_rise_but_not_below_what_is_used(
    sessionmaker: async_sessionmaker[AsyncSession],
    workspace_id: str,
    make_experiment: MakeExperiment,
) -> None:
    experiment = await make_experiment(BudgetLimits(money_micros=100))
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        budget = await budget_for_experiment(scope, experiment.id)
        await try_reserve(scope, budget.id, money_micros=80, tokens=0, requests=1, attempt_id=None)
        await session.refresh(budget)
        with pytest.raises(LimitBelowUsageError):
            await raise_limits(scope, budget, BudgetLimits(money_micros=50))
        with pytest.raises(LimitBelowUsageError):
            await raise_limits(scope, budget, BudgetLimits(currency="EUR", money_micros=500))
        await raise_limits(scope, budget, BudgetLimits(money_micros=500))
        assert budget.money_limit_micros == 500
