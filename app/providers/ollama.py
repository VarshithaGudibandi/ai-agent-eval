import json
import uuid
from typing import Any

import httpx

from app.providers.contract import (
    Fatal,
    GenerationSettings,
    Message,
    ModelResponse,
    ProviderCaps,
    PublicToolSchema,
    Retryable,
    ToolCallRequest,
    Usage,
)

DEFAULT_BASE_URL = "http://localhost:11434"


def ollama_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        item: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            item["tool_calls"] = [
                {"function": {"name": c.name, "arguments": c.arguments}} for c in message.tool_calls
            ]
        if message.role == "tool" and message.tool_name:
            item["tool_name"] = message.tool_name
        out.append(item)
    return out


def parse_response(body: dict[str, Any], requested_model: str) -> ModelResponse:
    message = body.get("message") or {}
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function", {})
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        calls.append(
            ToolCallRequest(
                id=f"call_{uuid.uuid4().hex[:12]}", name=function["name"], arguments=arguments
            )
        )
    known = "prompt_eval_count" in body and "eval_count" in body
    return ModelResponse(
        text=message.get("content") or "",
        tool_calls=calls,
        usage=Usage(
            input_tokens=body["prompt_eval_count"] if known else None,
            cached_input_tokens=0 if known else None,
            output_tokens=body["eval_count"] if known else None,
            other_tokens=0 if known else None,
        ),
        resolved_model=body.get("model") or requested_model,
        finish_reason=body.get("done_reason"),
    )


class OllamaProvider:
    """A model on the same machine. Nothing leaves it, so no key is needed."""

    def __init__(self, client: httpx.AsyncClient, base_url: str = DEFAULT_BASE_URL) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")

    async def generate(
        self,
        messages: list[Message],
        tools: list[PublicToolSchema],
        settings: GenerationSettings,
        timeout: float,
    ) -> ModelResponse:
        body = {
            "model": settings.model,
            "messages": ollama_messages(messages),
            "stream": False,
            "options": {
                "temperature": settings.temperature,
                "num_predict": settings.max_output_tokens,
            },
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ],
        }
        try:
            response = await self._client.post(
                f"{self._base_url}/api/chat", json=body, timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise Retryable("request timed out") from exc
        except httpx.TransportError as exc:
            raise Retryable(f"cannot reach Ollama: {type(exc).__name__}") from exc
        if response.status_code >= 500:
            raise Retryable(f"Ollama returned {response.status_code}")
        if response.status_code >= 400:
            raise Fatal(f"Ollama returned {response.status_code}: {response.text[:300]}")
        try:
            return parse_response(response.json(), settings.model)
        except (ValueError, KeyError) as exc:
            raise Fatal(f"unreadable Ollama response: {type(exc).__name__}") from exc

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(parallel_tool_calls=True, cached_tokens=False, max_output_tokens=8192)
