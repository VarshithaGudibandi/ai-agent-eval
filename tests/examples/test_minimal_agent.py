import asyncio
import importlib.util
import json
import threading
from collections.abc import AsyncIterator, Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import uvicorn
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.contract import RunLimits
from app.api.main import create_app
from app.auth.signing_secrets import rotate_signing_secret
from app.budget.pricing import create_price_version
from app.db import models
from app.db.content import TaskDefinition, create_agent_config, create_dataset_version
from app.db.experiments import create_experiment
from app.db.scope import WorkspaceScope
from app.environments.registry import default_registry
from app.evaluators.registry import default_evaluators
from app.execution.attempt_runner import Platform
from app.execution.experiment_runner import run_experiment
from app.providers.key_vault import key_vault_from_settings
from app.settings import Settings
from tests.datasets.reference import load_task
from tests.execution.conftest import PRICE, scripted_providers

Sessions = async_sessionmaker[AsyncSession]
AGENT_FILE = Path(__file__).parents[2] / "examples/minimal_agent/agent.py"
TASKS = ["refund_damaged_headphones", "lookup_order_shipped", "ineligible_return_in_transit"]


def _example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("minimal_agent", AGENT_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def gateway_url(settings: Settings) -> AsyncIterator[str]:
    """The real API, gateway included, listening on a local port."""
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=0, log_level="warning")
    )
    serving = asyncio.create_task(server.serve())
    # uvicorn exposes no event for startup, only this flag.
    while not server.started:  # noqa: ASYNC110
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await serving


def _serve(server: ThreadingHTTPServer) -> Iterator[str]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def example_agent() -> Iterator[tuple[str, ModuleType]]:
    module = _example()
    for url in _serve(module.serve(0, host="127.0.0.1")):
        yield url, module


async def _setup(
    sessionmaker: Sessions, workspace_id: str, settings: Settings, module: ModuleType
) -> None:
    async with sessionmaker.begin() as session:
        secret = await rotate_signing_secret(
            session, workspace_id, key_vault_from_settings(settings)
        )
    # The team copies the secret, shown once, into their agent's configuration.
    module.Handler.secret = secret


async def _experiment(
    sessionmaker: Sessions,
    workspace_id: str,
    tasks: list[TaskDefinition],
    adapter: models.AdapterType,
    config_settings: dict[str, Any],
) -> str:
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        version = await create_dataset_version(scope, f"d-{adapter}", tasks)
        if adapter == models.AdapterType.TOOL_LOOP:
            await create_price_version(scope, "scripted", "m", PRICE)
        config = await create_agent_config(scope, "agent", adapter, config_settings, RunLimits())
        experiment = await create_experiment(
            scope, version.id, [config.id], default_evaluators(), mode=models.ExperimentMode.REAL
        )
        return experiment.id


async def _graded(
    sessionmaker: Sessions, experiment_id: str
) -> list[tuple[models.Run, models.Attempt, dict[tuple[str, str], str], list[models.TraceEvent]]]:
    out = []
    async with sessionmaker() as session:
        runs = await session.scalars(
            select(models.Run).where(models.Run.experiment_id == experiment_id)
        )
        for run in runs:
            attempt = await session.scalar(
                select(models.Attempt).where(models.Attempt.run_id == run.id)
            )
            assert attempt is not None
            scores = {
                (s.evaluator_name, s.dimension): s.status
                for s in await session.scalars(
                    select(models.Score).where(models.Score.attempt_id == attempt.id)
                )
            }
            events = list(
                await session.scalars(
                    select(models.TraceEvent)
                    .where(models.TraceEvent.attempt_id == attempt.id)
                    .order_by(models.TraceEvent.sequence_number)
                )
            )
            out.append((run, attempt, scores, events))
    return out


def _as_dev(name: str) -> TaskDefinition:
    return load_task("support_retail", name).model_copy(update={"split": models.Split.DEV})


async def test_the_example_agent_is_graded_exactly_like_the_built_in_loop(
    sessionmaker: Sessions,
    workspace_id: str,
    settings: Settings,
    gateway_url: str,
    example_agent: tuple[str, ModuleType],
) -> None:
    agent_url, module = example_agent
    await _setup(sessionmaker, workspace_id, settings, module)
    platform = Platform(
        sessionmaker,
        default_registry(),
        default_evaluators(),
        providers=scripted_providers(),
        settings=settings.model_copy(
            update={"allow_local_agents": True, "gateway_public_url": gateway_url}
        ),
    )
    external = await _experiment(
        sessionmaker,
        workspace_id,
        [_as_dev(name) for name in TASKS],
        models.AdapterType.HTTP,
        {"url": agent_url},
    )
    built_in = await _experiment(
        sessionmaker,
        workspace_id,
        [_as_dev("refund_damaged_headphones")],
        models.AdapterType.TOOL_LOOP,
        {"provider": "scripted", "model": "m"},
    )
    assert await run_experiment(platform, workspace_id, external) == "completed"
    assert await run_experiment(platform, workspace_id, built_in) == "completed"

    http_runs = await _graded(sessionmaker, external)
    assert [(r.execution_status, r.evaluation_status) for r, *_ in http_runs] == [
        ("completed", "pass")
    ] * 3
    for _, attempt, _, events in http_runs:
        kinds = [e.event_type for e in events]
        # The agent acted only through the gateway, which traced every call.
        assert "tool_call_requested" in kinds and "external_agent_replied" in kinds
        assert attempt.run_token_hash is not None
        assert "run_" not in json.dumps([e.payload for e in events]).replace("run_id", "")

    [(_, _, loop_scores, _)] = await _graded(sessionmaker, built_in)
    [http_refund] = [
        scores
        for run, attempt, scores, _ in http_runs
        if "refunded" in (attempt.final_response or "")
    ]
    # Same task, same pipeline: the same evaluators grade the same dimensions the same way.
    assert http_refund == loop_scores


async def test_an_agent_that_ignores_its_tool_limit_is_stopped_by_the_gateway(
    sessionmaker: Sessions,
    workspace_id: str,
    settings: Settings,
    gateway_url: str,
) -> None:
    module = _example()

    def greedy(task: dict[str, Any], gateway: Any) -> str:
        for _ in range(15):
            gateway.call("get_order", order_id="ord_1001")
        return "Checked many times."

    module.decide = greedy
    for agent_url in _serve(module.serve(0, host="127.0.0.1")):
        await _setup(sessionmaker, workspace_id, settings, module)
        platform = Platform(
            sessionmaker,
            default_registry(),
            default_evaluators(),
            settings=settings.model_copy(
                update={"allow_local_agents": True, "gateway_public_url": gateway_url}
            ),
        )
        experiment = await _experiment(
            sessionmaker,
            workspace_id,
            [_as_dev("refund_damaged_headphones")],
            models.AdapterType.HTTP,
            {"url": agent_url},
        )
        await run_experiment(platform, workspace_id, experiment)
    [(run, attempt, _, _)] = await _graded(sessionmaker, experiment)
    assert (attempt.stop_reason, run.evaluation_status) == ("max_tool_calls", "fail")
    async with sessionmaker() as session:
        refused = await session.scalar(
            select(models.ToolCall.id).where(
                models.ToolCall.attempt_id == attempt.id,
                models.ToolCall.error_code == "TOOL_LIMIT_REACHED",
            )
        )
    assert refused is not None
