import os

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.contract import RunLimits
from app.db import models
from app.db.budgets import BudgetLimits, budget_for_experiment
from app.db.content import create_agent_config, create_dataset_version, load_task_file
from app.db.experiments import create_experiment
from app.db.scope import WorkspaceScope
from app.environments.registry import default_registry
from app.evaluators.registry import default_evaluators
from app.execution.attempt_runner import Platform
from app.execution.experiment_runner import run_experiment
from app.execution.launch import launch_prices
from app.providers.credentials import create_credential
from app.providers.key_vault import key_vault_from_settings
from app.providers.registry import default_providers
from app.settings import Settings
from tests.auth.helpers import owner_with_workspace
from tests.conftest import TASK_FILE

MODEL = os.environ.get("GEMINI_TEST_MODEL", "gemini-flash-lite-latest")


async def test_real_gemini_run_solves_the_refund_task_with_every_call_traced(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    key = os.environ["GEMINI_API_KEY"]
    async with sessionmaker.begin() as session:
        _, owner = await owner_with_workspace(session)
        scope = WorkspaceScope(session, owner.workspace_id)
        # Free-tier Flash Lite allows about 15 requests a minute.
        await create_credential(
            scope,
            owner,
            models.KeyedProvider.GEMINI,
            "paid test",
            key,
            key_vault_from_settings(settings),
            requests_per_minute=10,
        )
        version = await create_dataset_version(scope, "retail", [load_task_file(TASK_FILE)])
        config = await create_agent_config(
            scope,
            "gemini",
            models.AdapterType.TOOL_LOOP,
            {"provider": "gemini", "model": MODEL, "max_output_tokens": 1024},
            RunLimits(),
        )
        budget = BudgetLimits(tokens=200_000, requests=20)
        prices = await launch_prices(scope, [config.id], default_providers(), budget)
        experiment = await create_experiment(
            scope,
            version.id,
            [config.id],
            default_evaluators(),
            mode=models.ExperimentMode.REAL,
            budget=budget,
            prices=prices,
        )
    platform = Platform(sessionmaker, default_registry(), default_evaluators(), settings=settings)

    status = await run_experiment(platform, owner.workspace_id, experiment.id)

    async with sessionmaker() as session:
        scope = WorkspaceScope(session, owner.workspace_id)
        run = await scope.first(
            scope.query(models.Run).where(models.Run.experiment_id == experiment.id)
        )
        assert run is not None
        attempt = await scope.first(
            scope.query(models.Attempt).where(models.Attempt.run_id == run.id)
        )
        assert attempt is not None
        events = await scope.all(
            scope.query(models.TraceEvent)
            .where(models.TraceEvent.attempt_id == attempt.id)
            .order_by(models.TraceEvent.sequence_number)
        )
        calls = await scope.all(
            scope.query(models.ModelCall).where(models.ModelCall.attempt_id == attempt.id)
        )
        scores = await scope.all(
            scope.query(models.Score).where(models.Score.attempt_id == attempt.id)
        )
        spent = await budget_for_experiment(scope, experiment.id)
        ledger_tokens = await session.scalar(
            select(func.sum(models.SpendEntry.tokens)).where(
                models.SpendEntry.budget_id == spent.id
            )
        )

    summary = {s.dimension: (s.status, s.reason_code) for s in scores}
    print(f"\nmodel {MODEL} -> {[c.resolved_model for c in calls]}, scores {summary}")
    print(f"final response: {attempt.final_response!r}")
    assert status == "completed"
    assert (run.execution_status, run.evaluation_status) == ("completed", "pass"), summary
    ok_calls = [c for c in calls if c.status == "ok"]
    assert ok_calls and all(c.usage_known for c in ok_calls)
    assert {e.id for e in events if e.event_type == "model_call"} == {
        c.trace_event_id for c in calls
    }
    assert [e.payload["tool"] for e in events if e.event_type == "tool_call_completed"].count(
        "issue_refund"
    ) == 1
    assert spent.tokens_used == int(ledger_tokens or 0) > 0
    assert all(key not in str(e.payload) for e in events)
