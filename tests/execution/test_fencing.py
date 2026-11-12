import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.budget.reservations import BudgetGuard
from app.db import models
from app.db.budgets import BudgetLimits, budget_for_experiment
from app.db.jobs import Claim, LeaseLostError, cancel, claim_next, enqueue
from app.db.scope import WorkspaceScope
from tests.conftest import MakeExperiment

Sessions = async_sessionmaker[AsyncSession]
KIND = "fencing_test"


async def _claimed(sessionmaker: Sessions, workspace_id: str, subject: str) -> Claim:
    async with sessionmaker.begin() as session:
        await enqueue(WorkspaceScope(session, workspace_id), KIND, subject)
    async with sessionmaker.begin() as session:
        claim = await claim_next(session, "w1", 30, [KIND])
    assert claim is not None and claim.subject_id == subject
    return claim


async def _guard(
    sessionmaker: Sessions,
    workspace_id: str,
    make_experiment: MakeExperiment,
    claim: Claim,
    token: str,
) -> tuple[BudgetGuard, str]:
    experiment = await make_experiment(BudgetLimits())
    async with sessionmaker() as session:
        budget = await budget_for_experiment(WorkspaceScope(session, workspace_id), experiment.id)
    guard = BudgetGuard(sessionmaker, workspace_id, experiment.id, budget.id, claim.job_id, token)
    return guard, budget.id


async def _entries(sessionmaker: Sessions, budget_id: str) -> int:
    async with sessionmaker() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(models.SpendEntry)
            .where(models.SpendEntry.budget_id == budget_id)
        )
    return int(count or 0)


async def test_a_stale_worker_cannot_reserve_budget(
    sessionmaker: Sessions, workspace_id: str, make_experiment: MakeExperiment
) -> None:
    claim = await _claimed(sessionmaker, workspace_id, "trace_stale")
    stale, budget_id = await _guard(sessionmaker, workspace_id, make_experiment, claim, "stale")
    with pytest.raises(LeaseLostError):
        await stale.reserve(money_micros=100, tokens=100)
    assert await _entries(sessionmaker, budget_id) == 0
    live, _ = await _guard(sessionmaker, workspace_id, make_experiment, claim, claim.lease_token)
    entry = await live.reserve(money_micros=100, tokens=100)
    assert entry.job_id == claim.job_id


async def test_a_cancelled_job_can_no_longer_spend(
    sessionmaker: Sessions, workspace_id: str, make_experiment: MakeExperiment
) -> None:
    claim = await _claimed(sessionmaker, workspace_id, "trace_cancelled")
    guard, budget_id = await _guard(
        sessionmaker, workspace_id, make_experiment, claim, claim.lease_token
    )
    async with sessionmaker.begin() as session:
        assert await cancel(WorkspaceScope(session, workspace_id), "trace_cancelled") == 1
    with pytest.raises(LeaseLostError):
        await guard.reserve(money_micros=100, tokens=100)
    assert await _entries(sessionmaker, budget_id) == 0
