from typing import Any

import pytest

from app.agents.contract import RunLimits
from app.agents.scripted import Script, ScriptedAdapter
from app.environments.contract import AgentTaskInput
from app.gateway.contract import PublicToolSchema, ToolCallOutcome


class FakeTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def schemas(self) -> list[PublicToolSchema]:
        return []

    async def call(self, tool: str, arguments: dict[str, Any]) -> ToolCallOutcome:
        self.calls.append((tool, arguments))
        return ToolCallOutcome(tool_call_id=f"tc_{len(self.calls)}", status="error")


class NoEvents:
    async def record(self, event_type: str, payload: dict[str, Any]) -> str:
        return "evt"


SCRIPT = Script.model_validate(
    {
        "steps": [
            {"tool": "get_order", "arguments": {"order_id": "ord_1"}},
            {"tool": "issue_refund", "arguments": {"order_id": "ord_1", "amount_minor": 5}},
        ],
        "final_response": "Done.",
    }
)
TASK = AgentTaskInput(user_message="refund please")


async def test_replays_every_step_even_after_errors() -> None:
    tools = FakeTools()
    result = await ScriptedAdapter(SCRIPT).run(TASK, tools, NoEvents(), RunLimits())
    assert [name for name, _ in tools.calls] == ["get_order", "issue_refund"]
    assert (result.final_response, result.stop_reason) == ("Done.", "model_final")


@pytest.mark.parametrize(
    ("limits", "reason"),
    [(RunLimits(max_tool_calls=1), "max_tool_calls"), (RunLimits(max_turns=1), "max_turns")],
)
async def test_stops_at_hard_limits(limits: RunLimits, reason: str) -> None:
    tools = FakeTools()
    result = await ScriptedAdapter(SCRIPT).run(TASK, tools, NoEvents(), limits)
    assert len(tools.calls) == 1
    assert (result.final_response, result.stop_reason) == ("", reason)
