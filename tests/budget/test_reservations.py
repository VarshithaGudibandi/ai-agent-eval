import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.budget.reservations import BudgetExhaustedError, BudgetGuard
from app.db import models
from app.db.budgets import BudgetLimits, budget_for_experiment
from app.db.scope import WorkspaceScope
from tests.conftest import MakeExperiment


async def _guard(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, experiment: models.Experiment
) -> BudgetGuard:
    async with sessionmaker() as session:
        budget = await budget_for_experiment(WorkspaceScope(session, workspace_id), experiment.id)
    return BudgetGuard(sessionmaker, workspace_id, experiment.id, budget.id)


async def _state(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, experiment_id: str
) -> tuple[str, models.Budget, set[str]]:
    async with sessionmaker() as session:
        scope = WorkspaceScope(session, workspace_id)
        experiment = await scope.require(models.Experiment, experiment_id)
        budget = await budget_for_experiment(scope, experiment_id)
        actions = {e.action for e in await scope.all(scope.query(models.AuditEntry))}
        return experiment.status, budget, actions


async def test_warns_once_at_eighty_percent_and_pauses_before_running_dry(
    sessionmaker: async_sessionmaker[AsyncSession],
    workspace_id: str,
    make_experiment: MakeExperiment,
) -> None:
    experiment = await make_experiment(BudgetLimits(money_micros=1_000))
    guard = await _guard(sessionmaker, workspace_id, experiment)
    for spent in (300, 300):
        reservation = await guard.reserve(money_micros=400, tokens=10)
        async with sessionmaker.begin() as session:
            await guard.settle(session, reservation, money_micros=spent, tokens=10, requests=1)
    status, budget, actions = await _state(sessionmaker, workspace_id, experiment.id)
    assert (status, budget.warned_at) == ("running", None)
    reservation = await guard.reserve(money_micros=400, tokens=10)
    async with sessionmaker.begin() as session:
        await guard.settle(session, reservation, money_micros=250, tokens=10, requests=1)
    status, budget, actions = await _state(sessionmaker, workspace_id, experiment.id)
    assert budget.money_used_micros == 850
    assert budget.warned_at is not None
    assert status == "paused_budget"
    assert {"budget.warning", "experiment.paused"} <= actions


async def test_reservation_that_does_not_fit_pauses_and_raises(
    sessionmaker: async_sessionmaker[AsyncSession],
    workspace_id: str,
    make_experiment: MakeExperiment,
) -> None:
    experiment = await make_experiment(BudgetLimits(requests=0))
    guard = await _guard(sessionmaker, workspace_id, experiment)
    with pytest.raises(BudgetExhaustedError):
        await guard.reserve(money_micros=0, tokens=1)
    status, budget, _ = await _state(sessionmaker, workspace_id, experiment.id)
    assert (status, budget.requests_used) == ("paused_budget", 0)
