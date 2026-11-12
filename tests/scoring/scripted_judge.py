import json

from app.providers.contract import (
    GenerationSettings,
    Message,
    ModelResponse,
    PublicToolSchema,
    Usage,
)


class AnsweringJudge:
    """Stands in for Gemini as a judge: answers each question as told, with its quote."""

    def __init__(self, answers: dict[str, tuple[str, str]]) -> None:
        self.answers = answers
        self.calls = 0
        self.prompts: list[list[Message]] = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[PublicToolSchema],
        settings: GenerationSettings,
        timeout: float,
    ) -> ModelResponse:
        assert settings.json_output and settings.temperature == 0
        self.calls += 1
        self.prompts.append(messages)
        reply = {
            "answers": [
                {"id": qid, "answer": answer, "quote": quote}
                for qid, (answer, quote) in self.answers.items()
            ]
        }
        return ModelResponse(
            text=json.dumps(reply),
            tool_calls=[],
            usage=Usage(input_tokens=900, output_tokens=60),
            resolved_model="gemini-3.5-flash-lite-001",
        )
