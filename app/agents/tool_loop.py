import json
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.agents.contract import AgentResult, EventRecorder, RunLimits
from app.environments.contract import AgentTaskInput
from app.gateway.contract import ToolSession
from app.providers.contract import GenerationSettings, Message, ModelResponse, PublicToolSchema

DEFAULT_SYSTEM_PROMPT = (
    "You are a customer support agent. Use the tools to look things up and to act. "
    "Do only what the customer asked for and what the tools allow. Never claim an action "
    "happened unless a tool confirmed it. When you are done, reply to the customer."
)


class ToolLoopSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    system_prompt: str = Field(default=DEFAULT_SYSTEM_PROMPT, min_length=1, max_length=20_000)
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_output_tokens: int = Field(default=1024, gt=0)


class ModelClient(Protocol):
    async def generate(
        self, messages: list[Message], tools: list[PublicToolSchema], settings: GenerationSettings
    ) -> ModelResponse: ...


class ModelSource(Protocol):
    """Hands an adapter a ready model client for one attempt: metered, limited and traced."""

    async def client(self, provider: str) -> ModelClient: ...


def first_messages(settings: ToolLoopSettings, task: AgentTaskInput) -> list[Message]:
    messages = [Message(role="system", content=settings.system_prompt)]
    if task.context:
        messages.append(
            Message(
                role="system",
                content="Session context: " + json.dumps(task.context, sort_keys=True),
            )
        )
    messages.append(Message(role="user", content=task.user_message))
    return messages


class ToolLoopAdapter:
    """Our own agent: ask the model, run the tools it asks for, repeat until it answers."""

    def __init__(self, settings: ToolLoopSettings, models: ModelSource) -> None:
        self._settings = settings
        self._models = models

    async def run(
        self,
        task_input: AgentTaskInput,
        tools: ToolSession,
        events: EventRecorder,
        limits: RunLimits,
    ) -> AgentResult:
        model = await self._models.client(self._settings.provider)
        generation = GenerationSettings(
            model=self._settings.model,
            temperature=self._settings.temperature,
            max_output_tokens=min(self._settings.max_output_tokens, limits.max_output_tokens),
        )
        messages = first_messages(self._settings, task_input)
        schemas = tools.schemas
        tool_calls = 0
        for turn in range(limits.max_turns):
            response = await model.generate(messages, schemas, generation)
            messages.append(
                Message(role="assistant", content=response.text, tool_calls=response.tool_calls)
            )
            if not response.tool_calls:
                return AgentResult(response.text, "model_final", {"turns": turn + 1})
            for call in response.tool_calls:
                if tool_calls >= limits.max_tool_calls:
                    return AgentResult("", "max_tool_calls", {"turns": turn + 1})
                tool_calls += 1
                outcome = await tools.call(call.name, call.arguments)
                messages.append(
                    Message(
                        role="tool",
                        content=json.dumps(outcome.model_dump(exclude={"tool_call_id"})),
                        tool_call_id=call.id,
                        tool_name=call.name,
                    )
                )
        return AgentResult("", "max_turns", {"turns": limits.max_turns})
