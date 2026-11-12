from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class PublicToolSchema(BaseModel):
    """A tool as agents see it: name, description and JSON Schema for the arguments."""

    name: str
    description: str
    parameters: dict[str, Any]


class ToolCallRequest(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]
    # Opaque provider token that must be sent back with this call on the next turn.
    signature: str | None = None


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None


class GenerationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_output_tokens: int = Field(default=1024, gt=0)


class Usage(BaseModel):
    """Normalized token counts. None means the provider did not say, which is not zero."""

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    other_tokens: int | None = None

    @property
    def known(self) -> bool:
        return self.input_tokens is not None and self.output_tokens is not None

    def total(self) -> int:
        return sum(
            n or 0
            for n in (
                self.input_tokens,
                self.cached_input_tokens,
                self.output_tokens,
                self.other_tokens,
            )
        )


class ModelResponse(BaseModel):
    text: str
    tool_calls: list[ToolCallRequest]
    usage: Usage
    resolved_model: str
    finish_reason: str | None = None


class ProviderCaps(BaseModel):
    parallel_tool_calls: bool
    cached_tokens: bool
    max_output_tokens: int


class ProviderError(Exception):
    code = "provider_error"


class RateLimited(ProviderError):
    code = "rate_limited"

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class Retryable(ProviderError):
    code = "retryable"


class Fatal(ProviderError):
    code = "fatal"


class ContentBlocked(ProviderError):
    code = "content_blocked"


class ModelProvider(Protocol):
    """One model API. Errors are always one of the typed provider errors above."""

    async def generate(
        self,
        messages: list[Message],
        tools: list[PublicToolSchema],
        settings: GenerationSettings,
        timeout: float,
    ) -> ModelResponse:
        """Ask the model for its next message, which may contain tool calls."""
        ...

    def capabilities(self) -> ProviderCaps:
        """What this provider supports, such as parallel tool calls and cached tokens."""
        ...
