import asyncio
import logging
from datetime import datetime

import pytest

from agent_eval import current_trace_id, tag, traced
from agent_eval.recording import config_document, set_live_context
from tests.sdk.conftest import CONFIG, Collected, recording

SECRET = "sk-" + "x" * 30


@traced
def get_order(order_id: str) -> dict[str, str]:
    return {"id": order_id, "status": "delivered"}


@traced(kind="step", name="plan")
def plan(message: str) -> str:
    return "look up the order"


@traced
def handle(message: str) -> str:
    plan(message)
    order = get_order("ord_1")
    tag(session="sess-1", experiment="exp_fake")
    return f"Your order {order['id']} is {order['status']}."


def test_a_top_level_call_is_one_trace_with_nested_spans(collected: Collected) -> None:
    assert handle("where is my order?") == "Your order ord_1 is delivered."
    [trace] = collected
    spans = collected.spans()
    assert spans["handle"]["kind"] == "step" and "parent_id" not in spans["handle"]
    assert spans["plan"]["kind"] == "step" and spans["get_order"]["kind"] == "tool"
    assert spans["get_order"]["parent_id"] == spans["handle"]["id"]
    assert spans["get_order"]["input"] == {"order_id": "ord_1"}
    assert spans["handle"]["output"] == "Your order ord_1 is delivered."
    # Eval's tags cannot be claimed from live code.
    assert trace["tags"] == {"environment": "test", "session": "sess-1"}
    assert (trace["agent"], trace["label"], trace["config"]) == ("refund-bot", "v1", CONFIG)
    for span in trace["spans"]:
        assert datetime.fromisoformat(span["end"]) >= datetime.fromisoformat(span["start"])


def test_the_functions_own_exception_is_recorded_and_raised_unchanged(
    collected: Collected,
) -> None:
    class OutOfStockError(Exception):
        pass

    @traced(name="issue_refund")
    def issue_refund(order_id: str) -> None:
        raise OutOfStockError("no stock")

    @traced
    def handler() -> None:
        issue_refund("ord_1")

    with pytest.raises(OutOfStockError, match="no stock"):
        handler()
    assert collected.spans()["issue_refund"]["error"] == "OutOfStockError: no stock"


async def test_async_functions_are_traced_and_the_trace_id_is_visible(
    collected: Collected,
) -> None:
    seen: list[str | None] = []

    @traced(kind="tool")
    async def lookup(order_id: str) -> str:
        await asyncio.sleep(0)
        seen.append(current_trace_id())
        return order_id

    @traced
    async def handler() -> list[str]:
        seen.append(current_trace_id())
        return list(await asyncio.gather(lookup("a"), lookup("b")))

    assert await handler() == ["a", "b"]
    assert current_trace_id() is None
    [trace] = collected
    assert seen == [trace["trace_id"]] * 3
    assert len(trace["spans"]) == 3


def test_nothing_is_recorded_without_init() -> None:
    set_live_context(None)
    assert handle("hi").startswith("Your order")
    assert current_trace_id() is None


def test_awkward_arguments_and_results_never_break_the_call(collected: Collected) -> None:
    class Unprintable:
        def __repr__(self) -> str:
            raise RuntimeError("no repr")

    @traced
    def tool(thing: object, blob: bytes, huge: str) -> object:
        return Unprintable()

    result = tool(Unprintable(), b"\x00" * 10, "y" * 50_000)
    assert isinstance(result, Unprintable)
    [span] = collected[0]["spans"]
    assert span["input"]["thing"] == "<unrepresentable Unprintable>"
    assert span["input"]["blob"] == "<10 bytes>"
    assert len(span["input"]["huge"]) < 21_000
    assert span["output"] == "<unrepresentable Unprintable>"


def test_secrets_are_masked_and_a_broken_hook_hides_the_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    traces = Collected()

    class Wrapped:
        def __repr__(self) -> str:
            return f"Wrapped({SECRET})"

    def hook(value: object) -> object:
        if value == "boom":
            raise ValueError("hook bug")
        # A hook may hand back an object; its text is still masked.
        return Wrapped() if value == "wrap" else value

    recording(traces, redact=hook)
    try:

        @traced
        def tool(text: str) -> str:
            return text

        @traced
        def handler() -> None:
            tool(f"key {SECRET}")
            tool("boom")
            tool("wrap")

        with caplog.at_level(logging.WARNING, logger="agent_eval"):
            handler()
    finally:
        set_live_context(None)
    text = str(traces[0])
    assert SECRET not in text
    outputs = [s["output"] for s in traces[0]["spans"] if s["kind"] == "tool"]
    assert outputs == ["key [redacted]", "[redacted]", "Wrapped([redacted])"]
    assert "redact hook failed" in caplog.text and "hook bug" not in caplog.text


def test_config_with_functions_has_a_stable_fingerprint() -> None:
    config = {"model": "m", "tools": [get_order], "api_key": "not-really-a-key"}
    assert config_document(config) == config_document(dict(config))
    assert config_document(config)["tools"] == ["tests.sdk.test_recording.get_order"]
    assert config_document(config)["api_key"] == "[redacted]"
