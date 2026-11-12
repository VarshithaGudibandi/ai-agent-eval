import json
import re
import uuid
from typing import Any

import httpx

from app.providers.contract import (
    ContentBlocked,
    Fatal,
    GenerationSettings,
    Message,
    ModelResponse,
    ProviderCaps,
    PublicToolSchema,
    RateLimited,
    Retryable,
    ToolCallRequest,
    Usage,
)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"

# Gemini accepts only a subset of JSON Schema for tool parameters and rejects the rest.
_SCHEMA_KEYS = {
    "type",
    "description",
    "properties",
    "required",
    "items",
    "enum",
    "format",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "nullable",
    "anyOf",
}
_BLOCKED_FINISH = {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}
_RETRYABLE_STATUS = {500, 502, 503, 504}


def gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key == "properties":
            out[key] = {name: gemini_schema(sub) for name, sub in value.items()}
        elif key == "items":
            out[key] = gemini_schema(value)
        elif key == "anyOf":
            out[key] = [gemini_schema(sub) for sub in value]
        else:
            out[key] = value
    return out


def _tool_result(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except ValueError:
        return {"result": content}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def gemini_contents(messages: list[Message]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    system = [m.content for m in messages if m.role == "system"]
    contents: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue
        if message.role == "tool":
            part = {
                "functionResponse": {
                    "name": message.tool_name,
                    "response": _tool_result(message.content),
                }
            }
            # Answers to several calls made in one turn go back together in one turn.
            if (
                contents
                and contents[-1]["role"] == "user"
                and "functionResponse" in contents[-1]["parts"][0]
            ):
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
            continue
        parts: list[dict[str, Any]] = [{"text": message.content}] if message.content else []
        parts += [_function_call_part(call) for call in message.tool_calls]
        contents.append(
            {"role": "model" if message.role == "assistant" else "user", "parts": parts}
        )
    instruction = {"parts": [{"text": "\n\n".join(system)}]} if system else None
    return instruction, contents


def _function_call_part(call: ToolCallRequest) -> dict[str, Any]:
    part: dict[str, Any] = {"functionCall": {"name": call.name, "args": call.arguments}}
    # Gemini 3 rejects a function call sent back without the signature it came with.
    if call.signature:
        part["thoughtSignature"] = call.signature
    return part


def _retry_after(body: dict[str, Any]) -> float | None:
    for detail in body.get("error", {}).get("details", []):
        delay = detail.get("retryDelay")
        if isinstance(delay, str) and (match := re.fullmatch(r"(\d+(?:\.\d+)?)s", delay)):
            return float(match.group(1))
    return None


def _usage(metadata: dict[str, Any] | None) -> Usage:
    if not metadata or "promptTokenCount" not in metadata:
        return Usage()
    cached = metadata.get("cachedContentTokenCount", 0)
    return Usage(
        input_tokens=metadata["promptTokenCount"] - cached,
        cached_input_tokens=cached,
        output_tokens=metadata.get("candidatesTokenCount", 0),
        other_tokens=metadata.get("thoughtsTokenCount", 0)
        + metadata.get("toolUsePromptTokenCount", 0),
    )


def parse_response(body: dict[str, Any], requested_model: str) -> ModelResponse:
    block = body.get("promptFeedback", {}).get("blockReason")
    candidates = body.get("candidates") or []
    if block or not candidates:
        if block:
            raise ContentBlocked(f"prompt blocked: {block}")
        raise Retryable("response had no candidates")
    candidate = candidates[0]
    finish = candidate.get("finishReason")
    if finish in _BLOCKED_FINISH:
        raise ContentBlocked(f"response blocked: {finish}")
    texts: list[str] = []
    calls: list[ToolCallRequest] = []
    for part in candidate.get("content", {}).get("parts", []):
        if "functionCall" in part:
            call = part["functionCall"]
            calls.append(
                ToolCallRequest(
                    id=call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    name=call["name"],
                    arguments=call.get("args") or {},
                    signature=part.get("thoughtSignature"),
                )
            )
        elif "text" in part and not part.get("thought"):
            texts.append(part["text"])
    return ModelResponse(
        text="".join(texts),
        tool_calls=calls,
        usage=_usage(body.get("usageMetadata")),
        resolved_model=body.get("modelVersion") or requested_model,
        finish_reason=finish,
    )


class GeminiProvider:
    def __init__(
        self, api_key: str, client: httpx.AsyncClient, base_url: str = DEFAULT_BASE_URL
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._base_url = base_url.rstrip("/")

    async def generate(
        self,
        messages: list[Message],
        tools: list[PublicToolSchema],
        settings: GenerationSettings,
        timeout: float,
    ) -> ModelResponse:
        instruction, contents = gemini_contents(messages)
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": settings.temperature,
                "maxOutputTokens": settings.max_output_tokens,
            },
        }
        if settings.json_output:
            body["generationConfig"]["responseMimeType"] = "application/json"
        if instruction:
            body["systemInstruction"] = instruction
        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": gemini_schema(t.parameters),
                        }
                        for t in tools
                    ]
                }
            ]
        model = settings.model.removeprefix("models/")
        try:
            # The key goes in a header, never the URL, so it cannot leak into logged URLs.
            response = await self._client.post(
                f"{self._base_url}/v1beta/models/{model}:generateContent",
                json=body,
                headers={"x-goog-api-key": self._api_key},
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise Retryable("request timed out") from exc
        except httpx.TransportError as exc:
            raise Retryable(f"network error: {type(exc).__name__}") from exc
        payload = _json(response)
        if response.status_code == 429:
            raise RateLimited("rate limited by Gemini", _retry_after(payload))
        if response.status_code in _RETRYABLE_STATUS:
            raise Retryable(f"Gemini returned {response.status_code}")
        if response.status_code >= 400:
            message = payload.get("error", {}).get("message", "")
            raise Fatal(f"Gemini returned {response.status_code}: {message}"[:500])
        return parse_response(payload, model)

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(parallel_tool_calls=True, cached_tokens=True, max_output_tokens=65536)


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}
