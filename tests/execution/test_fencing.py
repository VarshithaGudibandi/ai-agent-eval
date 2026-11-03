from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.contract import RunLimits
from app.budget.reservations import BudgetGuard
from app.db import models
from app.db.attempts import AttemptNotLiveError
from app.db.budgets import budget_for_experiment
from app.db.content import create_agent_config, create_dataset_version, load_task_file
from app.db.experiments import create_experiment
from app.db.jobs import Claim, cancel_work, claim_next
from app.db.scope import WorkspaceScope
from app.environments.contract import TaskVersion
from app.environments.registry import default_registry
from app.environments.state_store import EnvStateStore
from app.environments.support_retail.environment import SupportRetail
from app.evaluators.contract import EvalContext, Layer, Score
from app.evaluators.registry import default_evaluators
from app.execution.attempt_runner import Platform, run_attempt
from app.execution.trace_recorder import TraceRecorder
from app.gateway.tool_gateway import AttemptContext, ToolGateway
from tests.conftest import TASK_FILE
from tests.environments.support_retail.fixtures import FIXTURE

Sessions = async_sessionmaker[AsyncSession]
APPROVAL = {"order_id": "ord_1001", "amount_minor": 24999, "reason": "damaged"}


async def _claimed(sessionmaker: Sessions, workspace_id: str, settings: dict[str, Any]) -> Claim:
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        version = await create_dataset_version(scope, "d", [load_task_file(TASK_FILE)])
        config = await create_agent_config(
            scope, "c", models.AdapterType.SCRIPTED, settings, RunLimits()
        )
        experiment = await create_experiment(
            scope, version.id, [config.id], [], mode=models.ExperimentMode.DEMO
        )
        claim = await claim_next(session, "w1", 30, experiment_id=experiment.id)
    assert claim is not None
    return claim


async def _expire(sessionmaker: Sessions, attempt_id: str) -> None:
    async with sessionmaker.begin() as session:
        await session.execute(
            update(models.Attempt)
            .where(models.Attempt.id == attempt_id)
            .values(lease_expires_at=func.now() - func.make_interval(0, 0, 0, 0, 0, 0, 1))
        )


async def _events(sessionmaker: Sessions, attempt_id: str) -> int:
    async with sessionmaker() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(models.TraceEvent)
            .where(models.TraceEvent.attempt_id == attempt_id)
        )
    return count or 0


async def test_a_stale_workers_tool_call_is_refused_and_changes_nothing(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    claim = await _claimed(sessionmaker, workspace_id, {})
    async with sessionmaker() as session:
        attempt = await session.get_one(models.Attempt, claim.attempt_id)
    async with sessionmaker.begin() as session:
        await SupportRetail(EnvStateStore(session, workspace_id)).setup(attempt.env_id, FIXTURE)
    ctx = AttemptContext(
        attempt_id=claim.attempt_id,
        workspace_id=workspace_id,
        env_id=attempt.env_id,
        environment="support-retail@1",
        task_context={"authenticated_customer_id": "cust_12"},
    )
    gateway = ToolGateway(sessionmaker, default_registry())
    holder = TraceRecorder(sessionmaker, workspace_id, claim.attempt_id, claim.lease_token)
    assert (await gateway.call(ctx, holder, "get_order", {"order_id": "ord_1001"})).status == "ok"
    before = await _events(sessionmaker, claim.attempt_id)

    impostor = TraceRecorder(sessionmaker, workspace_id, claim.attempt_id, "not-the-lease")
    with pytest.raises(AttemptNotLiveError):
        await gateway.call(ctx, impostor, "request_refund_approval", APPROVAL)
    await _expire(sessionmaker, claim.attempt_id)
    with pytest.raises(AttemptNotLiveError):
        await gateway.call(ctx, holder, "request_refund_approval", APPROVAL)

    assert await _events(sessionmaker, claim.attempt_id) == before
    async with sessionmaker() as session:
        state = await EnvStateStore(session, workspace_id).snapshot(attempt.env_id)
    assert state.get("approvals", []) == []


async def test_a_stale_worker_cannot_reserve_budget(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    claim = await _claimed(sessionmaker, workspace_id, {})
    async with sessionmaker() as session:
        run = await session.get_one(models.Run, claim.run_id)
        budget = await budget_for_experiment(
            WorkspaceScope(session, workspace_id), run.experiment_id
        )
    guard = BudgetGuard(
        sessionmaker, workspace_id, run.experiment_id, budget.id, claim.attempt_id, "stale"
    )
    with pytest.raises(AttemptNotLiveError):
        await guard.reserve(money_micros=100, tokens=100)
    async with sessionmaker() as session:
        entries = await session.scalar(
            select(func.count())
            .select_from(models.SpendEntry)
            .where(models.SpendEntry.budget_id == budget.id)
        )
    assert entries == 0


class _CancelsDuringGrading:
    """Stands in for the experiment being cancelled while a run is being graded."""

    name = "cancels"
    version = "0"
    layer: Layer = "outcome"

    def __init__(self, sessionmaker: Sessions, workspace_id: str, experiment_id: str) -> None:
        self.sessionmaker = sessionmaker
        self.workspace_id = workspace_id
        self.experiment_id = experiment_id

    def applies_to(self, task: TaskVersion) -> bool:
        return True

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        async with self.sessionmaker.begin() as session:
            await cancel_work(WorkspaceScope(session, self.workspace_id), self.experiment_id)
        return []


async def test_a_revoked_attempt_records_no_result(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    script = {
        "script": {
            "steps": [{"tool": "get_order", "arguments": {"order_id": "ord_1001"}}],
            "final_response": "Your order is on its way.",
        }
    }
    claim = await _claimed(sessionmaker, workspace_id, script)
    async with sessionmaker() as session:
        run = await session.get_one(models.Run, claim.run_id)
    platform = Platform(
        sessionmaker,
        default_registry(),
        [
            *default_evaluators(),
            _CancelsDuringGrading(sessionmaker, workspace_id, run.experiment_id),
        ],
    )
    with pytest.raises(AttemptNotLiveError):
        await run_attempt(platform, workspace_id, claim.attempt_id, claim.lease_token)
    async with sessionmaker() as session:
        attempt = await session.get_one(models.Attempt, claim.attempt_id)
        run = await session.get_one(models.Run, claim.run_id)
        scores = await session.scalar(
            select(func.count())
            .select_from(models.Score)
            .where(models.Score.attempt_id == claim.attempt_id)
        )
    assert (attempt.execution_status, run.execution_status) == ("cancelled", "cancelled")
    assert run.evaluation_status == "pending"
    assert scores == 0
