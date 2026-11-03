import asyncio
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.contract import RunLimits
from app.budget.pricing import create_price_version
from app.db import models
from app.db.attempts import AttemptNotLiveError
from app.db.budgets import BudgetLimits
from app.db.content import create_agent_config, create_dataset_version, load_task_file
from app.db.experiments import create_experiment
from app.db.ids import new_id
from app.db.jobs import claim_next
from app.db.scope import WorkspaceScope
from app.environments.registry import default_registry
from app.evaluators.registry import default_evaluators
from app.execution.attempt_runner import Platform, run_attempt
from app.execution.worker import Worker, run
from app.providers.contract import GenerationSettings, Message, ModelResponse
from app.providers.registry import ProviderEntry, ProviderRegistry
from app.providers.scripted import ScriptedProvider
from app.settings import Settings
from tests.conftest import TASK_FILE
from tests.execution.conftest import PRICE, refund_conversation, scripted_providers

Sessions = async_sessionmaker[AsyncSession]
ROOT = Path(__file__).parents[2]
CORRECT_SCRIPT = {
    "script": {
        "steps": [
            {"tool": "get_order", "arguments": {"order_id": "ord_1001"}},
            {"tool": "get_return_status", "arguments": {"order_id": "ord_1001"}},
            {
                "tool": "request_refund_approval",
                "arguments": {"order_id": "ord_1001", "amount_minor": 24999, "reason": "damaged"},
            },
            {
                "tool": "issue_refund",
                "arguments": {
                    "order_id": "ord_1001",
                    "amount_minor": 24999,
                    "approval_id": "apr_0001",
                },
            },
        ],
        "final_response": "I have refunded the full $249.99 to your card.",
    }
}


def _fast(settings: Settings, database_url: str, **changes: object) -> Settings:
    return settings.model_copy(
        update={
            "database_url": database_url,
            "worker_idle_seconds": 0.05,
            "lease_seconds": 2.0,
            "heartbeat_seconds": 0.5,
            "reconcile_seconds": 0.2,
            **changes,
        }
    )


async def _workspace(sessionmaker: Sessions) -> str:
    workspace = models.Workspace(id=new_id("ws"), name="worker")
    async with sessionmaker.begin() as session:
        session.add(workspace)
    return workspace.id


async def _experiment(
    sessionmaker: Sessions,
    workspace_id: str,
    *,
    tool_loop: bool,
    configs: int = 1,
    repetitions: int = 1,
) -> str:
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        version = await create_dataset_version(scope, "d", [load_task_file(TASK_FILE)])
        if tool_loop:
            await create_price_version(scope, "scripted", "m", PRICE)
            made = [
                await create_agent_config(
                    scope,
                    f"loop-{n}",
                    models.AdapterType.TOOL_LOOP,
                    {"provider": "scripted", "model": "m", "temperature": n / 10},
                    RunLimits(),
                )
                for n in range(configs)
            ]
        else:
            made = [
                await create_agent_config(
                    scope, "s", models.AdapterType.SCRIPTED, CORRECT_SCRIPT, RunLimits()
                )
            ]
        experiment = await create_experiment(
            scope,
            version.id,
            [c.id for c in made],
            default_evaluators(),
            mode=models.ExperimentMode.REAL if tool_loop else models.ExperimentMode.DEMO,
            repetitions=repetitions,
            budget=BudgetLimits(),
        )
        return experiment.id


class Gated(ScriptedProvider):
    """Replies like a capable model, but stops before one reply until the gate opens."""

    def __init__(self, gate: asyncio.Event, stop_before: int) -> None:
        super().__init__(refund_conversation())
        self.gate = gate
        self.stop_before = stop_before
        self.reached = asyncio.Event()
        self.made = 0

    async def generate(
        self,
        messages: list[Message],
        tools: list[object],
        settings: GenerationSettings,
        timeout: float,
    ) -> ModelResponse:
        if self.made == self.stop_before:
            self.reached.set()
            await self.gate.wait()
        self.made += 1
        return await super().generate(messages, tools, settings, timeout)  # type: ignore[arg-type]


def _gated_providers(made: list[Gated], gate: asyncio.Event, stop_before: int) -> ProviderRegistry:
    def build(key: object, settings: object, http: object) -> Gated:
        provider = Gated(gate, stop_before)
        made.append(provider)
        return provider

    return ProviderRegistry({"scripted": ProviderEntry(needs_key=False, build=build)})


async def _until(check: Callable[[], Awaitable[bool]], seconds: float = 20) -> None:
    async def poll() -> None:
        # The state lives in the database, so there is no event to wait on: poll it.
        while not await check():  # noqa: ASYNC110
            await asyncio.sleep(0.05)

    await asyncio.wait_for(poll(), timeout=seconds)


async def _status(sessionmaker: Sessions, experiment_id: str) -> str:
    async with sessionmaker() as session:
        return (await session.get_one(models.Experiment, experiment_id)).status


async def _attempts(sessionmaker: Sessions, experiment_id: str) -> list[models.Attempt]:
    async with sessionmaker() as session:
        return list(
            await session.scalars(
                select(models.Attempt)
                .join(models.Run, models.Run.id == models.Attempt.run_id)
                .where(models.Run.experiment_id == experiment_id)
                .order_by(models.Attempt.attempt_number)
            )
        )


async def test_worker_becomes_ready_and_stops_on_signal(
    settings: Settings, fresh_database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.execution.worker")
    stop = asyncio.Event()
    task = asyncio.create_task(run(_fast(settings, fresh_database_url), stop))
    await asyncio.sleep(0.5)
    assert not task.done(), "worker exited before being told to stop"
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    messages = [r.getMessage() for r in caplog.records]
    assert "worker ready" in messages
    assert messages[-1] == "worker stopped"


async def test_worker_waits_while_database_is_down(
    unreachable_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.execution.worker")
    stop = asyncio.Event()
    task = asyncio.create_task(run(unreachable_settings, stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    messages = [r.getMessage() for r in caplog.records]
    assert "waiting for database" in messages
    assert "worker ready" not in messages


async def test_a_worker_process_finishes_work_queued_before_it_started(
    fresh_sessionmaker: Sessions, fresh_database_url: str
) -> None:
    workspace_id = await _workspace(fresh_sessionmaker)
    experiment_id = await _experiment(fresh_sessionmaker, workspace_id, tool_loop=False)
    env = {
        **os.environ,
        "DATABASE_URL": fresh_database_url,
        "WORKER_IDLE_SECONDS": "0.1",
        "LOG_FORMAT": "text",
    }
    worker = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.execution.worker", cwd=ROOT, env=env
    )
    try:
        await _until(
            lambda: _equals(_status(fresh_sessionmaker, experiment_id), "completed"), seconds=30
        )
    finally:
        worker.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(worker.wait(), timeout=15) == 0
    [attempt] = await _attempts(fresh_sessionmaker, experiment_id)
    assert attempt.execution_status == "completed"


async def _equals(value: Awaitable[str], expected: str) -> bool:
    return await value == expected


async def test_a_killed_worker_leaves_one_interrupted_attempt_and_one_clean_replacement(
    fresh_sessionmaker: Sessions, fresh_database_url: str, settings: Settings
) -> None:
    fast = _fast(settings, fresh_database_url)
    workspace_id = await _workspace(fresh_sessionmaker)
    experiment_id = await _experiment(fresh_sessionmaker, workspace_id, tool_loop=True)

    # The first worker claims the run, issues the refund, then freezes before its final reply
    # and stops heartbeating: as good as killed.
    gate = asyncio.Event()
    frozen: list[Gated] = []
    stale = Platform(
        fresh_sessionmaker,
        default_registry(),
        default_evaluators(),
        providers=_gated_providers(frozen, gate, stop_before=3),
        settings=fast,
    )
    async with fresh_sessionmaker.begin() as session:
        claim = await claim_next(session, "frozen", 1.0, experiment_id=experiment_id)
    assert claim is not None
    stale_run = asyncio.create_task(
        run_attempt(stale, workspace_id, claim.attempt_id, claim.lease_token)
    )
    await _until(lambda: _reached(frozen))

    # A healthy worker reconciles the expired lease and reruns the run from scratch.
    healthy = Worker(
        Platform(
            fresh_sessionmaker,
            default_registry(),
            default_evaluators(),
            providers=scripted_providers(),
            settings=fast,
        ),
        fast,
        worker_id="healthy",
    )
    stop = asyncio.Event()
    serving = asyncio.create_task(healthy.run(stop))
    try:
        await _until(lambda: _equals(_status(fresh_sessionmaker, experiment_id), "completed"))
    finally:
        stop.set()
        await asyncio.wait_for(serving, timeout=10)

    # The frozen worker wakes up. Its next step is refused and writes nothing.
    gate.set()
    with pytest.raises(AttemptNotLiveError):
        await asyncio.wait_for(stale_run, timeout=10)

    first, second = await _attempts(fresh_sessionmaker, experiment_id)
    assert (first.execution_status, second.execution_status) == ("interrupted", "completed")
    assert (first.final_response, first.lease_token) == (None, None)
    assert second.env_id != first.env_id
    async with fresh_sessionmaker() as session:
        run_row = await session.get_one(models.Run, claim.run_id)
        scored = set(
            await session.scalars(
                select(models.Score.attempt_id).where(models.Score.workspace_id == workspace_id)
            )
        )
        snapshots = {
            (s.attempt_id, s.kind)
            for s in await session.scalars(
                select(models.EnvSnapshot).where(models.EnvSnapshot.workspace_id == workspace_id)
            )
        }
        finished = await session.scalar(
            select(func.count())
            .select_from(models.TraceEvent)
            .where(
                models.TraceEvent.attempt_id == first.id,
                models.TraceEvent.event_type == "agent_finished",
            )
        )
        evidence = list(
            await session.scalars(
                select(models.EnvRecord.collection).where(models.EnvRecord.env_id == first.env_id)
            )
        )
    assert (run_row.execution_status, run_row.evaluation_status) == ("completed", "pass")
    # Only the replacement is graded, so the refund the frozen worker made counts nowhere.
    assert scored == {second.id}
    assert (first.id, "final") not in snapshots and (second.id, "final") in snapshots
    assert finished == 0
    # The interrupted environment is kept as evidence, refund and all, never reused.
    assert "refunds" in evidence


async def _reached(made: list[Gated]) -> bool:
    return bool(made) and made[0].reached.is_set()


async def test_a_worker_runs_a_bounded_number_of_attempts_and_alternates_configs(
    fresh_sessionmaker: Sessions, fresh_database_url: str, settings: Settings
) -> None:
    fast = _fast(settings, fresh_database_url, worker_concurrency=2)
    workspace_id = await _workspace(fresh_sessionmaker)
    experiment_id = await _experiment(
        fresh_sessionmaker, workspace_id, tool_loop=True, configs=2, repetitions=3
    )
    gate = asyncio.Event()
    made: list[Gated] = []
    worker = Worker(
        Platform(
            fresh_sessionmaker,
            default_registry(),
            default_evaluators(),
            providers=_gated_providers(made, gate, stop_before=0),
            settings=fast,
        ),
        fast,
    )
    stop = asyncio.Event()
    serving = asyncio.create_task(worker.run(stop))
    try:
        await _until(lambda: _count_reached(made, 2))
        await asyncio.sleep(0.5)
        running = await _attempts(fresh_sessionmaker, experiment_id)
        assert len(running) == 2
        async with fresh_sessionmaker() as session:
            configs = {
                (await session.get_one(models.Run, a.run_id)).agent_config_id for a in running
            }
        assert len(configs) == 2
    finally:
        stop.set()
        gate.set()
        await asyncio.wait_for(serving, timeout=10)
    # A graceful stop finished what was in flight and started nothing new.
    assert [a.execution_status for a in await _attempts(fresh_sessionmaker, experiment_id)] == [
        "completed",
        "completed",
    ]


async def _count_reached(made: list[Gated], count: int) -> bool:
    return sum(1 for p in made if p.reached.is_set()) >= count
