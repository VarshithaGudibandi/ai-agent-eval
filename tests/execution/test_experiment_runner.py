from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.budgets import BudgetLimits, budget_for_experiment, raise_limits
from app.db.experiments import requeue_budget_stopped_runs, transition
from app.db.scope import WorkspaceScope
from app.execution.attempt_runner import Platform
from app.execution.experiment_runner import run_experiment
from app.providers.registry import ProviderRegistry
from tests.execution.conftest import (
    COST_PER_CALL,
    MakeToolLoopExperiment,
    scripted_providers,
)


async def _runs(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, experiment_id: str
) -> list[tuple[models.Run, list[models.Attempt], list[models.Score]]]:
    async with sessionmaker() as session:
        scope = WorkspaceScope(session, workspace_id)
        out = []
        for run in await scope.all(
            scope.query(models.Run)
            .where(models.Run.experiment_id == experiment_id)
            .order_by(models.Run.repetition)
        ):
            attempts = await scope.all(
                scope.query(models.Attempt)
                .where(models.Attempt.run_id == run.id)
                .order_by(models.Attempt.attempt_number)
            )
            scores = await scope.all(
                scope.query(models.Score).where(
                    models.Score.attempt_id.in_([a.id for a in attempts])
                )
            )
            out.append((run, attempts, scores))
        return out


async def test_tiny_budget_pauses_without_losing_finished_runs_then_resumes(
    sessionmaker: async_sessionmaker[AsyncSession],
    workspace_id: str,
    tool_loop_experiment: MakeToolLoopExperiment,
    platform_with: Callable[[ProviderRegistry], Platform],
) -> None:
    platform = platform_with(scripted_providers())
    # Each run makes 3 calls of 1,100 micros. 8,000 covers two runs and not a third.
    cap = 8_000
    made = await tool_loop_experiment(BudgetLimits(money_micros=cap), 4)

    status = await run_experiment(platform, workspace_id, made.experiment_id)

    assert status == models.ExperimentStatus.PAUSED_BUDGET
    runs = await _runs(sessionmaker, workspace_id, made.experiment_id)
    finished = [r for r, _, _ in runs if r.evaluation_status == "pass"]
    unfinished = [r for r, _, _ in runs if r.evaluation_status == "pending"]
    assert len(finished) == 2
    assert len(unfinished) == 2
    assert all(r.execution_status in ("queued", "budget_exceeded") for r in unfinished)
    assert all(not scores for r, _, scores in runs if r in unfinished)
    async with sessionmaker() as session:
        scope = WorkspaceScope(session, workspace_id)
        budget = await budget_for_experiment(scope, made.experiment_id)
        ledger = await session.scalar(
            select(func.sum(models.SpendEntry.money_micros)).where(
                models.SpendEntry.budget_id == budget.id
            )
        )
        paid_calls = await session.scalar(
            select(func.count())
            .select_from(models.ModelCall)
            .join(models.Attempt, models.Attempt.id == models.ModelCall.attempt_id)
            .join(models.Run, models.Run.id == models.Attempt.run_id)
            .where(models.Run.experiment_id == made.experiment_id, models.ModelCall.status == "ok")
        )
    assert budget.money_used_micros <= cap
    assert paid_calls is not None and paid_calls >= 6
    assert budget.money_used_micros == int(ledger or 0) == paid_calls * COST_PER_CALL
    before = {
        r.id: ([a.id for a in attempts], sorted(s.id for s in scores))
        for r, attempts, scores in runs
        if r in finished
    }

    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        budget = await budget_for_experiment(scope, made.experiment_id)
        await raise_limits(scope, budget, BudgetLimits(money_micros=100_000))
        await transition(
            scope,
            made.experiment_id,
            {models.ExperimentStatus.PAUSED_BUDGET},
            models.ExperimentStatus.QUEUED,
        )
        await requeue_budget_stopped_runs(scope, made.experiment_id)

    assert await run_experiment(platform, workspace_id, made.experiment_id) == "completed"
    runs = await _runs(sessionmaker, workspace_id, made.experiment_id)
    assert [r.evaluation_status for r, _, _ in runs] == ["pass"] * 4
    after = {
        r.id: ([a.id for a in attempts], sorted(s.id for s in scores))
        for r, attempts, scores in runs
        if r.id in before
    }
    assert after == before


async def test_every_model_call_is_traced_with_its_cost(
    sessionmaker: async_sessionmaker[AsyncSession],
    workspace_id: str,
    tool_loop_experiment: MakeToolLoopExperiment,
    platform_with: Callable[[ProviderRegistry], Platform],
) -> None:
    made = await tool_loop_experiment(BudgetLimits(), 1)
    await run_experiment(platform_with(scripted_providers()), workspace_id, made.experiment_id)
    [(run, [attempt], _)] = await _runs(sessionmaker, workspace_id, made.experiment_id)
    assert run.evaluation_status == "pass"
    async with sessionmaker() as session:
        events = list(
            await session.scalars(
                select(models.TraceEvent)
                .where(models.TraceEvent.attempt_id == attempt.id)
                .order_by(models.TraceEvent.sequence_number)
            )
        )
        calls = list(
            await session.scalars(
                select(models.ModelCall).where(models.ModelCall.attempt_id == attempt.id)
            )
        )
    kinds = [e.event_type for e in events]
    assert kinds == [
        "attempt_started",
        "model_call",
        "tool_call_requested",
        "tool_call_completed",
        "model_call",
        "tool_call_requested",
        "tool_call_completed",
        "model_call",
        "agent_finished",
    ]
    assert len(calls) == 3
    assert all(c.cost_micros == COST_PER_CALL and c.usage_known for c in calls)
    assert {e.id for e in events if e.event_type == "model_call"} == {
        c.trace_event_id for c in calls
    }


async def test_pausing_by_hand_stops_between_runs(
    sessionmaker: async_sessionmaker[AsyncSession],
    workspace_id: str,
    tool_loop_experiment: MakeToolLoopExperiment,
    platform_with: Callable[[ProviderRegistry], Platform],
) -> None:
    made = await tool_loop_experiment(BudgetLimits(), 3)
    async with sessionmaker.begin() as session:
        await transition(
            WorkspaceScope(session, workspace_id),
            made.experiment_id,
            {models.ExperimentStatus.QUEUED},
            models.ExperimentStatus.PAUSED_USER,
        )
    status = await run_experiment(
        platform_with(scripted_providers()), workspace_id, made.experiment_id
    )
    assert status == "paused_user"
    runs = await _runs(sessionmaker, workspace_id, made.experiment_id)
    assert all(not attempts for _, attempts, _ in runs)
