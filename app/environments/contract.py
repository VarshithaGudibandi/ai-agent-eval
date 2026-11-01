from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class AgentTaskInput(BaseModel):
    """What the agent is allowed to see. Never contains the fixture or expected outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_message: str
    context: dict[str, Any] = Field(default_factory=dict)


class TaskVersion(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    task_key: str
    environment: str
    scenario_family: str
    split: str
    agent_input: AgentTaskInput
    fixture: dict[str, Any]
    evaluator_spec: dict[str, Any]


class ToolArgs(BaseModel):
    """Base for tool arguments. Unknown fields and loose type coercion are both refused."""

    model_config = ConfigDict(extra="forbid", strict=True)


@dataclass(frozen=True)
class ToolSchema:
    name: str
    description: str
    args_model: type[ToolArgs]

    def parameters(self) -> dict[str, Any]:
        return self.args_model.model_json_schema()


class CallContext(BaseModel):
    """Server-side facts about a tool call. The agent cannot influence any of them."""

    model_config = ConfigDict(frozen=True)

    attempt_id: str
    workspace_id: str
    env_id: str
    tool_call_id: str
    task_context: dict[str, Any]


class StateChange(BaseModel):
    collection: str
    record_id: str
    operation: Literal["insert", "update", "delete"]
    data: dict[str, Any] | None = None


class ToolResult(BaseModel):
    status: Literal["ok", "error"]
    output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    state_changes: list[StateChange] = Field(default_factory=list)

    @classmethod
    def ok(cls, output: dict[str, Any], changes: list[StateChange] | None = None) -> "ToolResult":
        return cls(status="ok", output=output, state_changes=changes or [])

    @classmethod
    def error(cls, code: str, message: str) -> "ToolResult":
        return cls(status="error", error_code=code, output={"message": message})


class Environment(Protocol):
    """A versioned world an agent acts in. It owns its tools, state and snapshots."""

    name: str
    version: str

    def tool_schemas(self) -> list[ToolSchema]:
        """The tools this environment offers, with strict argument models."""
        ...

    def agent_view(self, task: TaskVersion) -> AgentTaskInput:
        """The part of the task the agent may see. Never the fixture."""
        ...

    async def setup(self, env_id: str, fixture: dict[str, Any]) -> None:
        """Create fresh state for one attempt from the task's hidden fixture."""
        ...

    async def execute(self, env_id: str, tool: str, args: ToolArgs, ctx: CallContext) -> ToolResult:
        """Run one validated tool call against the attempt's state."""
        ...

    async def snapshot(self, env_id: str) -> dict[str, Any]:
        """The full current state, for grading and for the final-state diff."""
        ...

    async def teardown(self, env_id: str) -> None:
        """Remove live state once it has been snapshotted."""
        ...
