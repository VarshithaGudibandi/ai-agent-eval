from collections.abc import Iterator
from typing import Any

import pytest

from agent_eval.recording import RunContext, set_live_context
from agent_eval.redact import RedactHook
from app.ingest.trace_format import Trace

CONFIG = {
    "model": "gemini-3.5-flash-lite",
    "prompt": "You are a refund agent.",
    "tools": [{"name": "get_order"}],
}


class Collected(list[dict[str, Any]]):
    """Finished traces, each checked against the platform's own trace format."""

    def deliver(self, trace: dict[str, Any]) -> None:
        Trace.model_validate(trace)
        self.append(trace)

    def spans(self, index: int = -1) -> dict[str, dict[str, Any]]:
        return {s["name"]: s for s in self[index]["spans"]}


def recording(collected: Collected, redact: RedactHook | None = None) -> None:
    set_live_context(
        RunContext(
            agent="refund-bot",
            config=CONFIG,
            label="v1",
            tags={"environment": "test"},
            deliver=collected.deliver,
            redact=redact,
        )
    )


@pytest.fixture
def collected() -> Iterator[Collected]:
    traces = Collected()
    recording(traces)
    yield traces
    set_live_context(None)
