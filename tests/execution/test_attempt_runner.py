from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.contract import AgentResult, RunLimits
from app.budget.pricing import create_price_version
from app.db import models
from app.db.attempts import AttemptNotLiveError
from app.db.budgets import BudgetLimits
from app.db.content import create_agent_config, create_dataset_version, load_task_file
from app.db.experiments import create_experiment, start_attempt
from app.db.scope import WorkspaceScope
from app.environments.contract import TaskVersion
from app.environments.registry import default_registry
from app.evaluators.contract import EvalContext, Layer, Score
from app.evaluators.registry import default_evaluators
from app.execution import attempt_runner
from app.execution.attempt_runner import Platform, run_attempt
from app.execution.experiment_runner import run_experiment
from app.providers.contract import Retryable
from app.settings import Settings
from tests.execution.conftest import PRICE, scripted_providers

TASK_FILE = (
    Path(__file__).parents[2] / "datasets/support_retail/tasks/refund_damaged_headphones.json"
)


def _script(amount_minor: int, final_response: str) -> dict[str, Any]:
    refund = {"order_id": "ord_1001", "amount_minor": amount_minor}
    return {
        "script": {
            "steps": [
                {"tool": "get_order", "arguments": {"order_id": "ord_1001"}},
                {"tool": "get_return_status", "arguments": {"order_id": "ord_1001"}},
                {
                    "tool": "request_refund_approval",
                    "arguments": {**refund, "reason": "Arrived damaged"},
                },
                {"tool": "issue_refund", "arguments": {**refund, "approval_id": "apr_0001"}},
            ],
            "final_response": final_response,
        }
    }


CORRECT = _script(24999, "I've refunded the full $249.99 to your card.")
WRONG_AMOUNT = _script(2499, "I've refunded the full $249.99 to your card.")


@pytest.fixture
def platform(sessionmaker: async_sessionmaker[AsyncSession]) -> Platform:
    return Platform(sessionmaker, default_registry(), default_evaluators())


async def _run_one(
    platform: Platform,
    workspace_id: str,
    settings: dict[str, Any],
    limits: RunLimits | None = None,
) -> tuple[models.EvaluationStatus, models.Attempt]:
    async with platform.sessionmaker.begin() as session:
        version = await create_dataset_version(
            WorkspaceScope(session, workspace_id), "support-retail", [load_task_file(TASK_FILE)]
        )
        config = await create_agent_config(
            WorkspaceScope(session, workspace_id),
            "scripted",
            models.AdapterType.SCRIPTED,
            settings,
            limits or RunLimits(),
        )
        experiment = await create_experiment(
            WorkspaceScope(session, workspace_id),
            version.id,
            [config.id],
            platform.evaluators,
            mode=models.ExperimentMode.DEMO,
        )
        run = await session.scalar(
            select(models.Run).where(models.Run.experiment_id == experiment.id)
        )
        assert run is not None
        attempt = await start_attempt(WorkspaceScope(session, workspace_id), run.id)
    status = await run_attempt(platform, workspace_id, attempt.id)
    async with platform.sessionmaker() as session:
        return status, await session.get_one(models.Attempt, attempt.id)


async def _scores(
    sessionmaker: async_sessionmaker[AsyncSession], attempt_id: str
) -> dict[str, models.Score]:
    async with sessionmaker() as session:
        rows = await session.scalars(
            select(models.Score).where(models.Score.attempt_id == attempt_id)
        )
        return {r.dimension: r for r in rows}


async def test_correct_script_passes(platform: Platform, workspace_id: str) -> None:
    status, attempt = await _run_one(platform, workspace_id, CORRECT)
    assert status == models.EvaluationStatus.PASS
    assert (attempt.execution_status, attempt.stop_reason) == ("completed", "model_final")
    scores = await _scores(platform.sessionmaker, attempt.id)
    assert (scores["refund_amount"].status, scores["refund_approval"].status) == ("pass", "pass")
    assert {s.status for s in scores.values()} <= {"pass", "not_applicable"}
    async with platform.sessionmaker() as session:
        run = await session.get_one(models.Run, attempt.run_id)
        experiment = await session.get_one(models.Experiment, run.experiment_id)
    assert (run.execution_status, run.evaluation_status) == ("completed", "pass")
    assert experiment.status == "completed"


async def test_wrong_amount_fails_and_points_at_the_refund_call(
    platform: Platform, workspace_id: str
) -> None:
    status, attempt = await _run_one(platform, workspace_id, WRONG_AMOUNT)
    assert status == models.EvaluationStatus.FAIL
    score = (await _scores(platform.sessionmaker, attempt.id))["refund_amount"]
    assert (score.status, score.reason_code) == ("fail", "WRONG_REFUND_AMOUNT")
    assert score.evidence["expected_amount_minor"] == 24999
    assert score.evidence["observed_amount_minor"] == 2499
    async with platform.sessionmaker() as session:
        refund_call = await session.scalar(
            select(models.ToolCall).where(
                models.ToolCall.attempt_id == attempt.id,
                models.ToolCall.tool_name == "issue_refund",
            )
        )
        assert refund_call is not None
        event = await session.get_one(models.TraceEvent, refund_call.result_event_id)
    assert score.evidence["event_ids"] == [event.id]
    assert event.payload["tool_call_id"] == refund_call.id
    assert event.payload["state_changes"][0]["data"]["amount_minor"] == 2499


async def test_agent_sees_its_input_but_never_the_hidden_parts(
    platform: Platform, workspace_id: str
) -> None:
    _, attempt = await _run_one(platform, workspace_id, CORRECT)
    async with platform.sessionmaker() as session:
        events = list(
            await session.scalars(
                select(models.TraceEvent)
                .where(models.TraceEvent.attempt_id == attempt.id)
                .order_by(models.TraceEvent.sequence_number)
            )
        )
    assert [e.event_type for e in events] == [
        "attempt_started",
        *["tool_call_requested", "tool_call_completed"] * 4,
        "agent_finished",
    ]
    started = events[0].payload
    assert started["agent_input"]["context"] == {"authenticated_customer_id": "cust_12"}
    assert set(started) == {"agent_input", "tools", "limits"}
    assert "cust_99" not in str(started)


async def test_snapshots_are_kept_and_live_state_is_removed(
    platform: Platform, workspace_id: str
) -> None:
    _, attempt = await _run_one(platform, workspace_id, CORRECT)
    async with platform.sessionmaker() as session:
        snapshots = {
            s.kind: s.state
            for s in await session.scalars(
                select(models.EnvSnapshot).where(models.EnvSnapshot.attempt_id == attempt.id)
            )
        }
        live = await session.scalar(
            select(models.EnvRecord).where(models.EnvRecord.env_id == attempt.env_id)
        )
    assert snapshots["initial"]["refunds"] == []
    assert [r["amount_minor"] for r in snapshots["final"]["refunds"]] == [24999]
    assert live is None


async def test_hitting_a_hard_limit_counts_as_unsuccessful(
    platform: Platform, workspace_id: str
) -> None:
    status, attempt = await _run_one(platform, workspace_id, CORRECT, RunLimits(max_tool_calls=1))
    assert status == models.EvaluationStatus.FAIL
    assert (attempt.execution_status, attempt.stop_reason) == ("completed", "max_tool_calls")


class _CrashingEvaluator:
    name = "crashes"
    version = "0.0.1"
    layer: Layer = "outcome"

    def applies_to(self, task: TaskVersion) -> bool:
        return True

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        raise RuntimeError("bug")


async def test_evaluator_crash_is_an_evaluation_error_not_a_verdict(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    platform = Platform(
        sessionmaker, default_registry(), [*default_evaluators(), _CrashingEvaluator()]
    )
    status, attempt = await _run_one(platform, workspace_id, CORRECT)
    assert status == models.EvaluationStatus.EVALUATION_ERROR
    scores = await _scores(sessionmaker, attempt.id)
    assert scores["evaluation"].reason_code == "EVALUATOR_CRASHED"
    assert scores["refund_amount"].status == "pass"


async def test_an_attempt_runs_only_once(platform: Platform, workspace_id: str) -> None:
    _, attempt = await _run_one(platform, workspace_id, CORRECT)
    with pytest.raises(AttemptNotLiveError, match="not queued"):
        await run_attempt(platform, workspace_id, attempt.id)


async def test_unreachable_model_leaves_the_run_unscored(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, settings: Settings
) -> None:
    flaky = scripted_providers(lambda: [Retryable("503")] * 10)
    no_retries = settings.model_copy(update={"model_call_max_retries": 0})
    platform = Platform(
        sessionmaker, default_registry(), default_evaluators(), providers=flaky, settings=no_retries
    )
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        await create_price_version(scope, "scripted", "m", PRICE)
        version = await create_dataset_version(scope, "d", [load_task_file(TASK_FILE)])
        config = await create_agent_config(
            scope,
            "loop",
            models.AdapterType.TOOL_LOOP,
            {"provider": "scripted", "model": "m"},
            RunLimits(),
        )
        experiment = await create_experiment(
            scope,
            version.id,
            [config.id],
            platform.evaluators,
            mode=models.ExperimentMode.REAL,
            budget=BudgetLimits(),
        )
    await run_experiment(platform, workspace_id, experiment.id)
    async with sessionmaker() as session:
        run = await session.scalar(
            select(models.Run).where(models.Run.experiment_id == experiment.id)
        )
        assert run is not None
        attempt_id = await session.scalar(
            select(models.Attempt.id).where(models.Attempt.run_id == run.id)
        )
    assert (run.execution_status, run.evaluation_status) == ("infrastructure_error", "pending")
    assert attempt_id is not None
    assert await _scores(sessionmaker, attempt_id) == {}


async def test_usage_an_agent_reports_is_kept_in_the_trace(
    platform: Platform, workspace_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Reporting:
        async def run(self, *_: Any) -> AgentResult:
            return AgentResult("Done.", "model_final", {"usage_self_reported": {"tokens": 42}})

    monkeypatch.setattr(attempt_runner, "build_adapter", lambda *_: Reporting())
    _, attempt = await _run_one(platform, workspace_id, CORRECT)
    async with platform.sessionmaker() as session:
        finished = await session.scalar(
            select(models.TraceEvent).where(
                models.TraceEvent.attempt_id == attempt.id,
                models.TraceEvent.event_type == "agent_finished",
            )
        )
    assert finished is not None
    assert finished.payload["usage_self_reported"] == {"tokens": 42}
