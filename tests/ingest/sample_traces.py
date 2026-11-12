from typing import Any

# A support agent's run as the SDK would send it: one root step, two model calls and a tool
# call, with a provider key leaking out of a tool result.

LEAKED_KEY = "AIza" + "Sy" + "K" * 33
CONFIG = {
    "model": "gemini-3.5-flash-lite",
    "prompt": "You are a careful support agent. Never refund without approval.",
    "tools": [{"name": "get_order", "parameters": {"order_id": "string"}}],
    "settings": {"temperature": 0},
}


def support_trace(trace_id: str = "trace-001", **changes: Any) -> dict[str, Any]:
    trace: dict[str, Any] = {
        "trace_id": trace_id,
        "agent": "support-bot",
        "config": CONFIG,
        "label": "v1",
        "tags": {"environment": "production", "session": "sess-42", "customer_tier": "gold"},
        "spans": [
            {
                "id": "root",
                "kind": "step",
                "name": "handle_request",
                "start": "2026-11-12T10:00:00.000Z",
                "end": "2026-11-12T10:00:02.500Z",
                "input": {"message": "Where is my order ord_1001?"},
                "output": {"reply": "Your order ord_1001 shipped yesterday."},
            },
            {
                "id": "m1",
                "parent_id": "root",
                "kind": "model",
                "name": "plan",
                "start": "2026-11-12T10:00:00.100Z",
                "end": "2026-11-12T10:00:00.900Z",
                "provider": "gemini",
                "model": "gemini-3.5-flash-lite",
                "input_tokens": 1000,
                "cached_input_tokens": 200,
                "output_tokens": 50,
                "time_to_first_token_ms": 300,
                "output": {"tool_calls": [{"name": "get_order", "arguments": {"id": "ord_1001"}}]},
            },
            {
                "id": "t1",
                "parent_id": "root",
                "kind": "tool",
                "name": "get_order",
                "start": "2026-11-12T10:00:01.000Z",
                "end": "2026-11-12T10:00:01.200Z",
                "input": {"order_id": "ord_1001"},
                "output": {"status": "shipped", "debug": f"called with key={LEAKED_KEY}"},
            },
            {
                "id": "m2",
                "parent_id": "root",
                "kind": "model",
                "name": "answer",
                "start": "2026-11-12T10:00:01.300Z",
                "end": "2026-11-12T10:00:02.400Z",
                "provider": "gemini",
                "model": "gemini-3.5-flash-lite",
                "input_tokens": 1500,
                "output_tokens": 30,
            },
        ],
    }
    trace.update(changes)
    return trace


# 1 micro per input token, 0.5 per cached input token and 4 per output token.
PRICE = {
    "provider": "gemini",
    "model": "gemini-3.5-flash-lite",
    "input_per_million_micros": 1_000_000,
    "cached_input_per_million_micros": 500_000,
    "output_per_million_micros": 4_000_000,
    "other_per_million_micros": 4_000_000,
}
# m1: 1000 + 200 * 0.5 + 50 * 4 = 1300. m2: 1500 + 30 * 4 = 1620.
EXPECTED_COST = 2920
