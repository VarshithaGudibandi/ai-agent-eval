import pytest

from app.providers.contract import GenerationSettings, ModelResponse, RateLimited, Usage
from app.providers.scripted import ScriptedProvider

SETTINGS = GenerationSettings(model="m")
REPLY = ModelResponse(text="hi", tool_calls=[], usage=Usage(), resolved_model="m")


async def test_replays_in_order_then_runs_out() -> None:
    provider = ScriptedProvider([RateLimited("slow down"), REPLY])
    with pytest.raises(RateLimited):
        await provider.generate([], [], SETTINGS, timeout=1)
    assert (await provider.generate([], [], SETTINGS, timeout=1)).text == "hi"
    with pytest.raises(Exception, match="ran out"):
        await provider.generate([], [], SETTINGS, timeout=1)
    assert len(provider.calls) == 3
