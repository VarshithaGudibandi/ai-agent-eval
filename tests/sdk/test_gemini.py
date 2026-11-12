import logging
from typing import Any

import pytest

from agent_eval import traced, wrap_gemini
from tests.sdk.conftest import Collected
from tests.sdk.fake_gemini import FakeGemini, Response, reply

TOOLS = [{"function_declarations": [{"name": "get_order", "parameters": {"type": "object"}}]}]


def _answer(model: str, contents: Any, config: Any) -> Response:
    return reply("Checking.", ("get_order", {"order_id": "ord_1"}))


def test_a_model_call_becomes_a_model_span_with_its_tokens(collected: Collected) -> None:
    client = wrap_gemini(FakeGemini(_answer))

    @traced
    def handler() -> Response:
        return client.models.generate_content(
            model="models/gemini-3.5-flash-lite",
            contents="where is ord_1?",
            config={"system_instruction": "You are a refund agent.", "tools": TOOLS},
        )

    response = handler()
    assert response.candidates[0].content.parts[0].text == "Checking."
    span = collected.spans()["generate_content"]
    assert (span["kind"], span["provider"], span["model"]) == (
        "model",
        "gemini",
        "gemini-3.5-flash-lite",
    )
    assert span["input"]["contents"] == "where is ord_1?"
    assert span["output"] == {
        "text": "Checking.",
        "tool_calls": [{"name": "get_order", "arguments": {"order_id": "ord_1"}}],
    }
    # Gemini counts cached tokens inside the prompt; the platform prices them apart.
    assert (span["input_tokens"], span["cached_input_tokens"]) == (100, 20)
    assert (span["output_tokens"], span["other_tokens"]) == (15, 5)
    # Everything else on the client passes through.
    assert client.api_version == "v1beta"


def test_a_call_unlike_the_config_warns_once(
    collected: Collected, caplog: pytest.LogCaptureFixture
) -> None:
    client = wrap_gemini(FakeGemini(_answer))

    @traced
    def handler() -> None:
        for _ in range(3):
            client.models.generate_content(
                model="gemini-other",
                contents="hi",
                config={"system_instruction": "Something else.", "tools": TOOLS},
            )

    with caplog.at_level(logging.WARNING, logger="agent_eval"):
        handler()
    messages = [r.getMessage() for r in caplog.records]
    assert sum("'gemini-other'" in m for m in messages) == 1
    assert sum("system prompt differs" in m for m in messages) == 1
    assert not any("tools" in m for m in messages)


async def test_async_calls_and_model_errors(collected: Collected) -> None:
    def failing(model: str, contents: Any, config: Any) -> Response:
        if contents == "fail":
            raise ConnectionError("model down")
        return reply("ok")

    client = wrap_gemini(FakeGemini(failing))

    @traced
    async def handler() -> None:
        await client.aio.models.generate_content(model="gemini-3.5-flash-lite", contents="hi")
        with pytest.raises(ConnectionError, match="model down"):
            await client.aio.models.generate_content(model="gemini-3.5-flash-lite", contents="fail")

    await handler()
    models = [s for s in collected[0]["spans"] if s["kind"] == "model"]
    assert [s.get("error") for s in models] == [None, "ConnectionError: model down"]
    assert models[0]["output"] == {"text": "ok"}


def test_a_reply_of_an_unexpected_shape_is_still_returned(collected: Collected) -> None:
    odd = object()
    client = wrap_gemini(FakeGemini(lambda *_: odd))  # type: ignore[arg-type,return-value]
    assert client.models.generate_content(model="m", contents="hi") is odd
    span = collected.spans()["generate_content"]
    assert span["output"] == {"text": None} and "input_tokens" not in span
