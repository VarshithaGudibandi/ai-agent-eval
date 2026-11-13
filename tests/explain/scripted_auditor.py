import json
import re

from app.providers.contract import (
    GenerationSettings,
    Message,
    ModelResponse,
    PublicToolSchema,
    Usage,
)


class ScriptedAuditor:
    """Stands in for the auditor model: names a cause and cites the spans it was shown,
    by the labels in its prompt, as a real model would. `invent` cites spans that were not."""

    def __init__(self, cause: str, invent: bool = False) -> None:
        self.cause = cause
        self.invent = invent
        self.prompts: list[list[Message]] = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[PublicToolSchema],
        settings: GenerationSettings,
        timeout: float,
    ) -> ModelResponse:
        self.prompts.append(messages)
        shown = sorted(set(re.findall(r"\[(r\d+/[^ \]]+) final reply\]", messages[1].content)))
        spans = ["r9/nope", "r1/missing"] if self.invent else shown
        reply = {
            "cause": self.cause,
            "confidence": "high",
            "claims": [{"text": "The run ends in an error instead of a reply.", "spans": spans}],
            "next_step": "Restore the removed sentence and run the eval again.",
        }
        return ModelResponse(
            text=json.dumps(reply),
            tool_calls=[],
            usage=Usage(input_tokens=3000, output_tokens=200),
            resolved_model="gemini-3.8-flash-001",
        )
