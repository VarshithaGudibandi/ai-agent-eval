import json
from typing import Any

import httpx
import pytest

from app.gateway.contract import PublicToolSchema
from app.providers.contract import (
    ContentBlocked,
    Fatal,
    GenerationSettings,
    Message,
    RateLimited,
    Retryable,
    ToolCallRequest,
)
from app.providers.gemini import GeminiProvider, gemini_contents, gemini_schema

SETTINGS = GenerationSettings(model="gemini-test", max_output_tokens=256)
TOOL = PublicToolSchema(
    name="get_order",
    description="Look up an order",
    parameters={
        "title": "GetOrderArgs",
        "type": "object",
        "additionalProperties": False,
        "properties": {"order_id": {"title": "Order Id", "type": "string", "minLength": 1}},
        "required": ["order_id"],
    },
)
CONVERSATION = [
    Message(role="system", content="Be helpful."),
    Message(role="user", content="Refund ord_1"),
    Message(
        role="assistant",
        tool_calls=[
            ToolCallRequest(id="c1", name="get_order", arguments={"order_id": "ord_1"}),
            ToolCallRequest(id="c2", name="get_order", arguments={"order_id": "ord_2"}),
        ],
    ),
    Message(role="tool", content='{"status": "ok"}', tool_call_id="c1", tool_name="get_order"),
    Message(role="tool", content="not json", tool_call_id="c2", tool_name="get_order"),
]


def _provider(handler: Any) -> tuple[GeminiProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return GeminiProvider("AIza-test-key", client, "https://gemini.test"), seen


def test_schema_keeps_only_what_gemini_accepts() -> None:
    assert gemini_schema(TOOL.parameters) == {
        "type": "object",
        "properties": {"order_id": {"type": "string", "minLength": 1}},
        "required": ["order_id"],
    }


def test_conversation_maps_to_gemini_turns() -> None:
    instruction, contents = gemini_contents(CONVERSATION)
    assert instruction == {"parts": [{"text": "Be helpful."}]}
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert len(contents[1]["parts"]) == 2
    responses = [p["functionResponse"] for p in contents[2]["parts"]]
    assert responses == [
        {"name": "get_order", "response": {"status": "ok"}},
        {"name": "get_order", "response": {"result": "not json"}},
    ]


async def test_request_and_response_round_trip() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "thinking", "thought": True},
                                {"text": "Looking it up."},
                                {
                                    "functionCall": {
                                        "name": "get_order",
                                        "args": {"order_id": "ord_1"},
                                    }
                                },
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 120,
                    "cachedContentTokenCount": 20,
                    "candidatesTokenCount": 30,
                    "thoughtsTokenCount": 5,
                },
                "modelVersion": "gemini-test-001",
            },
        )

    provider, seen = _provider(ok)
    response = await provider.generate(CONVERSATION[:2], [TOOL], SETTINGS, timeout=5)
    [request] = seen
    assert request.url.path == "/v1beta/models/gemini-test:generateContent"
    assert request.headers["x-goog-api-key"] == "AIza-test-key"
    assert "key" not in str(request.url)
    body = json.loads(request.content)
    assert body["generationConfig"] == {"temperature": 0.0, "maxOutputTokens": 256}
    assert body["tools"][0]["functionDeclarations"][0]["name"] == "get_order"
    assert response.text == "Looking it up."
    assert [(c.name, c.arguments) for c in response.tool_calls] == [
        ("get_order", {"order_id": "ord_1"})
    ]
    assert response.usage.model_dump() == {
        "input_tokens": 100,
        "cached_input_tokens": 20,
        "output_tokens": 30,
        "other_tokens": 5,
    }
    assert response.resolved_model == "gemini-test-001"


async def test_missing_usage_is_unknown_not_zero() -> None:
    provider, _ = _provider(
        lambda r: httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
        )
    )
    response = await provider.generate(CONVERSATION[:2], [], SETTINGS, timeout=5)
    assert not response.usage.known
    assert response.usage.input_tokens is None


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            httpx.Response(
                429,
                json={"error": {"details": [{"@type": "x.RetryInfo", "retryDelay": "13s"}]}},
            ),
            RateLimited,
        ),
        (httpx.Response(503, json={}), Retryable),
        (httpx.Response(400, json={"error": {"message": "bad schema"}}), Fatal),
        (httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}), ContentBlocked),
        (
            httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY", "content": {}}]}),
            ContentBlocked,
        ),
    ],
)
async def test_errors_are_typed(response: httpx.Response, error: type[Exception]) -> None:
    provider, _ = _provider(lambda r: response)
    with pytest.raises(error) as caught:
        await provider.generate(CONVERSATION[:2], [], SETTINGS, timeout=5)
    if isinstance(caught.value, RateLimited):
        assert caught.value.retry_after_seconds == 13


async def test_network_failures_are_retryable() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider, _ = _provider(down)
    with pytest.raises(Retryable):
        await provider.generate(CONVERSATION[:2], [], SETTINGS, timeout=5)


async def test_errors_never_contain_the_key() -> None:
    provider, _ = _provider(lambda r: httpx.Response(403, json={"error": {"message": "denied"}}))
    with pytest.raises(Fatal) as caught:
        await provider.generate(CONVERSATION[:2], [], SETTINGS, timeout=5)
    assert "AIza" not in str(caught.value)
