from app.agents.contract import RunLimits


def test_tighter_limits_take_the_smaller_of_each() -> None:
    a = RunLimits(max_turns=5, max_tool_calls=30, timeout_seconds=10)
    b = RunLimits(max_turns=8, max_tool_calls=3, timeout_seconds=60)
    assert a.tighter(b) == RunLimits(max_turns=5, max_tool_calls=3, timeout_seconds=10)
