from collections.abc import Sequence

from app.gateway.contract import PublicToolSchema
from app.providers.contract import (
    GenerationSettings,
    Message,
    ModelResponse,
    ProviderCaps,
    ProviderError,
)


class ScriptedProvider:
    """Answers with a fixed list of responses or errors, one per call. For tests and demos."""

    def __init__(self, replies: Sequence[ModelResponse | ProviderError]) -> None:
        self._replies = list(replies)
        self.calls: list[list[Message]] = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[PublicToolSchema],
        settings: GenerationSettings,
        timeout: float,
    ) -> ModelResponse:
        self.calls.append(list(messages))
        if not self._replies:
            raise ProviderError("scripted provider ran out of replies")
        reply = self._replies.pop(0)
        if isinstance(reply, ProviderError):
            raise reply
        return reply

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(parallel_tool_calls=True, cached_tokens=False, max_output_tokens=8192)
