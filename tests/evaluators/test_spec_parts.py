import pytest
from pydantic import ValidationError

from app.evaluators.spec_parts import CallPattern, Claim
from tests.evaluators.traces import Trace


def test_call_pattern_matches_a_subset_of_arguments() -> None:
    trace = Trace()
    trace.call("get_order", {"order_id": "ord_1001"})
    [call] = trace.context({"expected_refunds": []}).tool_calls()
    assert CallPattern(tool="get_order").matches(call)
    assert CallPattern(tool="get_order", args={"order_id": "ord_1001"}).matches(call)
    assert not CallPattern(tool="get_order", args={"order_id": "ord_2"}).matches(call)
    assert CallPattern(tool="get_order", args={"order_id": "ord_1001"}).label() == (
        "get_order(order_id=ord_1001)"
    )


def test_claims_need_an_upper_case_code_and_valid_patterns() -> None:
    with pytest.raises(ValidationError):
        Claim(name="x", code="lower", any_of=["a"])
    with pytest.raises(ValidationError):
        Claim(name="x", code="OK", any_of=["("])
