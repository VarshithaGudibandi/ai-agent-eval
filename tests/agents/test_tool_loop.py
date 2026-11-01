import json
from typing import Any

from app.agents.contract import RunLimits
from app.agents.tool_loop import ToolLoopAdapter, ToolLoopSettings, first_messages
from app.environments.contract import AgentTaskInput
from app.gateway.contract import PublicToolSchema, ToolCallOutcome
from app.providers.contract import (
    GenerationSettings,
    Message,
    ModelResponse,
    ToolCallRequest,
    Usage,
)

SETTINGS = ToolLoopSettings(provider="scripted", model="m", max_output_tokens=2000)
TASK = AgentTaskInput(user_message="refund ord_1", context={"authenticated_customer_id": "c1"})


def _call(name: str, n: int) -> ModelResponse:
    return ModelResponse(
        text="",
        tool_calls=[ToolCallRequest(id=f"c{n}", name=name, arguments={"order_id": "ord_1"})],
        usage=Usage(),
        resolved_model="m",
    )


FINAL = ModelResponse(text="Done.", tool_calls=[], usage=Usage(), resolved_model="m")


class FakeModel:
    def __init__(self, replies: list[ModelResponse]) -> None:
        self.replies = replies
        self.seen: list[tuple[list[Message], GenerationSettings]] = []

    async def generate(
        self, messages: list[Message], tools: list[PublicToolSchema], settings: GenerationSettings
    ) -> ModelResponse:
        self.seen.append((list(messages), settings))
        return self.replies.pop(0)


class FakeSource:
    def __init__(self, model: FakeModel) -> None:
        self.model = model

    async def client(self, provider: str) -> FakeModel:
        return self.model


class FakeTools:
    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def schemas(self) -> list[PublicToolSchema]:
        return []

    async def call(self, tool: str, arguments: dict[str, Any]) -> ToolCallOutcome:
        self.calls.append(tool)
        return ToolCallOutcome(tool_call_id="tc", status="ok", output={"tool": tool})


class NoEvents:
    async def record(self, event_type: str, payload: dict[str, Any]) -> str:
        return "evt"


async def _run(replies: list[ModelResponse], limits: RunLimits) -> tuple[Any, FakeModel, FakeTools]:
    model, tools = FakeModel(replies), FakeTools()
    result = await ToolLoopAdapter(SETTINGS, FakeSource(model)).run(TASK, tools, NoEvents(), limits)
    return result, model, tools


async def test_runs_requested_tools_and_feeds_results_back() -> None:
    result, model, tools = await _run(
        [_call("get_order", 1), _call("issue_refund", 2), FINAL], RunLimits()
    )
    assert (result.final_response, result.stop_reason) == ("Done.", "model_final")
    assert tools.calls == ["get_order", "issue_refund"]
    last_messages, settings = model.seen[-1]
    tool_messages = [m for m in last_messages if m.role == "tool"]
    assert [m.tool_call_id for m in tool_messages] == ["c1", "c2"]
    assert json.loads(tool_messages[0].content)["output"] == {"tool": "get_order"}
    assert settings.max_output_tokens == 2000


async def test_output_tokens_are_capped_by_the_run_limits() -> None:
    _, model, _ = await _run([FINAL], RunLimits(max_output_tokens=300))
    assert model.seen[0][1].max_output_tokens == 300


async def test_stops_at_max_turns_and_max_tool_calls() -> None:
    looping = [_call("get_order", n) for n in range(10)]
    result, _, tools = await _run(list(looping), RunLimits(max_turns=3))
    assert (result.stop_reason, len(tools.calls)) == ("max_turns", 3)
    result, _, tools = await _run(list(looping), RunLimits(max_tool_calls=2))
    assert (result.stop_reason, len(tools.calls)) == ("max_tool_calls", 2)


def test_agent_sees_the_context_but_as_its_own_message() -> None:
    messages = first_messages(SETTINGS, TASK)
    assert [m.role for m in messages] == ["system", "system", "user"]
    assert "authenticated_customer_id" in messages[1].content
    assert messages[2].content == "refund ord_1"
