from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class PublicToolSchema(BaseModel):
    """A tool as agents see it: name, description and JSON Schema for the arguments."""

    name: str
    description: str
    parameters: dict[str, Any]


class ToolCallOutcome(BaseModel):
    """What the agent gets back from one tool call."""

    tool_call_id: str
    status: Literal["ok", "error", "rejected"]
    output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None


class ToolSession(Protocol):
    """The only way an agent can act. Every call goes through the tool gateway."""

    @property
    def schemas(self) -> list[PublicToolSchema]:
        """The tools this attempt may call."""
        ...

    async def call(self, tool: str, arguments: dict[str, Any]) -> ToolCallOutcome:
        """Call one tool. Rejections and errors come back as outcomes, never exceptions."""
        ...
