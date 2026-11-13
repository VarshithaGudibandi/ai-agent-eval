import os

import httpx

from app.providers.contract import GenerationSettings, Message, PublicToolSchema
from app.providers.gemini import GeminiProvider

MODEL = os.environ.get("GEMINI_TEST_MODEL", "gemini-3.5-flash-lite")
LOOKUP = PublicToolSchema(
    name="get_order",
    description="Look up an order by its id.",
    parameters={
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"],
    },
)


async def test_a_real_tool_call_round_trip_through_gemini() -> None:
    settings = GenerationSettings(model=MODEL, max_output_tokens=256)
    async with httpx.AsyncClient() as client:
        provider = GeminiProvider(os.environ["GEMINI_API_KEY"], client)
        messages = [
            Message(role="system", content="Use the tools. Answer in one short sentence."),
            Message(role="user", content="What is the status of order ord_1001?"),
        ]
        first = await provider.generate(messages, [LOOKUP], settings, timeout=60)
        [call] = first.tool_calls
        assert (call.name, call.arguments) == ("get_order", {"order_id": "ord_1001"})
        assert first.usage.known and first.resolved_model
        # Newer models reject the next turn unless the call's signature is sent back with it.
        messages += [
            Message(role="assistant", tool_calls=[call]),
            Message(
                role="tool",
                tool_call_id=call.id,
                tool_name=call.name,
                content='{"order_id": "ord_1001", "status": "shipped"}',
            ),
        ]
        second = await provider.generate(messages, [LOOKUP], settings, timeout=60)
    assert "shipped" in second.text.lower()
