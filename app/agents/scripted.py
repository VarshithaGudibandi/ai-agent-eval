from typing import Any

from pydantic import BaseModel, ConfigDict

from app.agents.contract import AgentResult, EventRecorder, RunLimits
from app.environments.contract import AgentTaskInput
from app.gateway.contract import ToolSession


class ScriptStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    arguments: dict[str, Any]


class Script(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[ScriptStep]
    final_response: str


class ScriptedAdapter:
    """Replays a fixed list of tool calls, then gives a fixed answer.

    It ignores what the tools return, which is the point: a script that refunds the
    wrong amount keeps doing so, and the evaluators must catch it.
    """

    def __init__(self, script: Script) -> None:
        self._script = script

    async def run(
        self,
        task_input: AgentTaskInput,
        tools: ToolSession,
        events: EventRecorder,
        limits: RunLimits,
    ) -> AgentResult:
        for index, step in enumerate(self._script.steps):
            # Each scripted call stands in for one model turn that asked for a tool.
            if index >= limits.max_tool_calls:
                return AgentResult("", "max_tool_calls", {"steps_run": index})
            if index >= limits.max_turns:
                return AgentResult("", "max_turns", {"steps_run": index})
            await tools.call(step.tool, step.arguments)
        return AgentResult(
            self._script.final_response, "model_final", {"steps_run": len(self._script.steps)}
        )
