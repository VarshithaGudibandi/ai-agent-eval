import json

import httpx
import pytest

from app.providers.contract import Fatal, GenerationSettings, Message, PublicToolSchema, Retryable
from app.providers.ollama import OllamaProvider

SETTINGS = GenerationSettings(model="llama-test", max_output_tokens=128)
TOOL = PublicToolSchema(name="get_order", description="d", parameters={"type": "object"})


def _provider(handler: httpx.MockTransport) -> OllamaProvider:
    return OllamaProvider(httpx.AsyncClient(transport=handler), "http://ollama.test")


async def test_tool_call_and_usage_are_parsed() -> None:
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "llama-test:latest",
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "get_order", "arguments": {"order_id": "o"}}}
                    ],
                },
                "prompt_eval_count": 40,
                "eval_count": 7,
                "done_reason": "stop",
            },
        )

    response = await _provider(httpx.MockTransport(ok)).generate(
        [Message(role="user", content="hi")], [TOOL], SETTINGS, timeout=5
    )
    body = json.loads(seen[0].content)
    assert (body["stream"], body["options"]["num_predict"]) == (False, 128)
    assert body["tools"][0]["function"]["name"] == "get_order"
    assert [c.arguments for c in response.tool_calls] == [{"order_id": "o"}]
    assert (response.usage.input_tokens, response.usage.output_tokens) == (40, 7)
    assert response.resolved_model == "llama-test:latest"


async def test_errors_are_typed() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(Retryable):
        await _provider(httpx.MockTransport(down)).generate([], [], SETTINGS, timeout=5)
    with pytest.raises(Fatal):
        await _provider(
            httpx.MockTransport(lambda r: httpx.Response(404, text="no model"))
        ).generate([], [], SETTINGS, timeout=5)
