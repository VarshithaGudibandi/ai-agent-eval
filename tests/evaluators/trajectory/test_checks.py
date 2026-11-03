from app.evaluators.scores import Named
from app.evaluators.spec_parts import CallPattern, OrderRule
from app.evaluators.trajectory.checks import call_order, repetition, required_calls
from tests.evaluators.traces import Trace


class _Grader:
    name = "grader"
    version = "1"
    layer = "trajectory"


GRADER: Named = _Grader()  # type: ignore[assignment]


def _calls(*pairs: tuple[str, dict[str, str]]) -> Trace:
    trace = Trace()
    for tool, args in pairs:
        trace.call(tool, args)
    return trace


def test_order_rules_match_on_the_named_argument_only_when_asked() -> None:
    trace = _calls(("lookup_user", {"user_id": "u1"}), ("reset_password", {"user_id": "u2"}))
    calls = trace.context({"expected_refunds": []}).tool_calls()
    loose = OrderRule(tool="reset_password", after=["lookup_user"])
    strict = OrderRule(tool="reset_password", after=["lookup_user"], match_arg="user_id")
    assert call_order(GRADER, [loose], calls).status == "pass"
    assert call_order(GRADER, [strict], calls).reason_code == "MISSING_PREREQUISITE"


def test_required_calls_and_repetition_work_for_any_tool_names() -> None:
    trace = _calls(*[("lookup_user", {"user_id": "u1"})] * 3)
    ctx = trace.context({"expected_refunds": []})
    calls = ctx.tool_calls()
    need = [
        CallPattern(tool="lookup_user", args={"user_id": "u1"}),
        CallPattern(tool="route_ticket"),
    ]
    score = required_calls(GRADER, ctx, need, calls)
    assert (score.reason_code, score.score) == ("MISSING_REQUIRED_CALL", 0.5)
    assert repetition(GRADER, 2, calls).reason_code == "EXCESSIVE_REPETITION"
    assert repetition(GRADER, 3, calls).status == "pass"
