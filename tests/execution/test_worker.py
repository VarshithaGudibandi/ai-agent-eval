import asyncio
import logging

import pytest

from app.execution.worker import run
from app.settings import Settings


async def _run_briefly(settings: Settings, seconds: float) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(run(settings, stop))
    await asyncio.sleep(seconds)
    assert not task.done(), "worker exited before being told to stop"
    stop.set()
    await asyncio.wait_for(task, timeout=5)


async def test_worker_becomes_ready_and_stops_on_signal(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.execution.worker")
    await _run_briefly(settings, 0.5)
    messages = [r.getMessage() for r in caplog.records]
    assert "worker ready" in messages
    assert messages[-1] == "worker stopped"


async def test_worker_waits_while_database_is_down(
    unreachable_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.execution.worker")
    await _run_briefly(unreachable_settings, 0.3)
    messages = [r.getMessage() for r in caplog.records]
    assert "waiting for database" in messages
    assert "worker ready" not in messages
