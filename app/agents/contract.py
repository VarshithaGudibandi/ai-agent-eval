from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.environments.contract import AgentTaskInput
from app.gateway.contract import ToolSession

StopReason = Literal[
    "model_final", "max_turns", "max_tool_calls", "timeout", "budget", "agent_error"
]

# Stopping for any of these means the run is unsuccessful, whatever the final state.
HARD_STOPS: frozenset[str] = frozenset({"max_turns", "max_tool_calls", "timeout", "budget"})


class RunLimits(BaseModel):
    """Hard stops for one run. Hitting any of them ends the run as unsuccessful."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_turns: int = Field(default=20, gt=0)
    max_tool_calls: int = Field(default=20, gt=0)
    timeout_seconds: float = Field(default=120, gt=0)
    max_output_tokens: int = Field(default=4096, gt=0)

    def tighter(self, other: "RunLimits") -> "RunLimits":
        return RunLimits(
            max_turns=min(self.max_turns, other.max_turns),
            max_tool_calls=min(self.max_tool_calls, other.max_tool_calls),
            timeout_seconds=min(self.timeout_seconds, other.timeout_seconds),
            max_output_tokens=min(self.max_output_tokens, other.max_output_tokens),
        )


class EventRecorder(Protocol):
    """Adds agent-side events, such as model calls, to the attempt's trace."""

    async def record(self, event_type: str, payload: dict[str, Any]) -> str:
        """Append one event and return its id."""
        ...


@dataclass
class AgentResult:
    final_response: str
    stop_reason: StopReason
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentAdapter(Protocol):
    """Runs one agent on one task. It may act only through the tool session."""

    async def run(
        self,
        task_input: AgentTaskInput,
        tools: ToolSession,
        events: EventRecorder,
        limits: RunLimits,
    ) -> AgentResult:
        """Run until the agent gives a final answer or hits a limit."""
        ...
