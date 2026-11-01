from typing import Any

from app.evaluators.contract import Score
from app.evaluators.trajectory.retail_trajectory import RetailTrajectory
from tests.evaluators.traces import REFUND_SPEC, Trace, by_dimension, correct_refund


async def _scores(trace: Trace, spec: dict[str, Any] = REFUND_SPEC) -> dict[str, Score]:
    return by_dimension(await RetailTrajectory().evaluate(trace.context(spec)))


def _statuses(scores: dict[str, Score]) -> dict[str, str]:
    return {d: s.status for d, s in scores.items()}


async def test_correct_refund_passes_and_has_nothing_to_handle() -> None:
    assert _statuses(await _scores(correct_refund())) == {
        "required_calls": "pass",
        "call_order": "pass",
        "repetition": "pass",
        "error_handling": "not_applicable",
    }


async def test_investigating_in_the_other_order_is_just_as_good() -> None:
    trace = Trace()
    trace.call("get_return_status", {"order_id": "ord_1001"})
    trace.call("get_order", {"order_id": "ord_1001"})
    approval = trace.approve("ord_1001", 24999)
    trace.refund("ord_1001", 24999, approval, number=2)
    assert _statuses(await _scores(trace))["call_order"] == "pass"


async def test_skipped_investigation_misses_a_call_and_breaks_the_order() -> None:
    trace = Trace()
    trace.call("get_order", {"order_id": "ord_1001"})
    approval = trace.approve("ord_1001", 24999)
    trace.refund("ord_1001", 24999, approval, number=2)
    scores = await _scores(trace)
    required, order = scores["required_calls"], scores["call_order"]
    assert (required.reason_code, required.score) == ("MISSING_REQUIRED_CALL", 0.5)
    assert required.evidence["missing"] == ["get_return_status(order_id=ord_1001)"]
    assert order.reason_code == "MISSING_PREREQUISITE"
    assert order.evidence["calls"][0]["missing"] == ["get_return_status"]


async def test_investigating_after_the_refund_is_out_of_order() -> None:
    trace = Trace()
    trace.call("get_order", {"order_id": "ord_1001"})
    approval = trace.approve("ord_1001", 24999)
    refund_call = trace.refund("ord_1001", 24999, approval, number=2)
    trace.call("get_return_status", {"order_id": "ord_1001"})
    scores = await _scores(trace)
    assert scores["required_calls"].status == "pass"
    assert scores["call_order"].reason_code == "MISSING_PREREQUISITE"
    ctx_calls = {c.tool_call_id: c for c in trace.context(REFUND_SPEC).tool_calls()}
    assert scores["call_order"].evidence["event_ids"] == [ctx_calls[refund_call].request_event_id]


async def test_looking_up_another_order_does_not_count() -> None:
    trace = Trace()
    trace.call("get_order", {"order_id": "ord_1001"})
    trace.call("get_return_status", {"order_id": "ord_1002"})
    approval = trace.approve("ord_1001", 24999)
    trace.refund("ord_1001", 24999, approval, number=2)
    assert (await _scores(trace))["call_order"].status == "fail"


async def test_one_call_satisfies_one_requirement() -> None:
    spec = {
        **REFUND_SPEC,
        "required_calls": [{"tool": "get_order", "args": {"order_id": "ord_1001"}}] * 2,
    }
    once = Trace()
    once.call("get_order", {"order_id": "ord_1001"})
    twice = Trace()
    twice.call("get_order", {"order_id": "ord_1001"})
    twice.call("get_order", {"order_id": "ord_1001"})
    assert (await _scores(once, spec))["required_calls"].status == "fail"
    assert (await _scores(twice, spec))["required_calls"].status == "pass"


async def test_failed_calls_do_not_satisfy_requirements() -> None:
    trace = Trace()
    trace.call("get_order", {"order_id": "ord_1001"}, status="error", error_code="TIMEOUT")
    trace.call("get_return_status", {"order_id": "ord_1001"})
    assert (await _scores(trace))["required_calls"].status == "fail"


async def test_too_many_identical_calls() -> None:
    trace = Trace()
    for _ in range(4):
        trace.call("get_order", {"order_id": "ord_1001"})
    score = (await _scores(trace))["repetition"]
    assert (score.reason_code, score.evidence["repeated"][0]["count"]) == (
        "EXCESSIVE_REPETITION",
        4,
    )
    assert len(score.evidence["event_ids"]) == 4


async def test_a_first_try_and_two_retries_are_allowed() -> None:
    trace = Trace()
    for _ in range(3):
        trace.call("get_order", {"order_id": "ord_1001"})
    assert (await _scores(trace))["repetition"].status == "pass"


def _unknown_refund(trace: Trace) -> None:
    trace.call("get_order", {"order_id": "ord_1001"})
    trace.call("get_return_status", {"order_id": "ord_1001"})
    approval = trace.approve("ord_1001", 24999)
    trace.refund(
        "ord_1001",
        24999,
        approval,
        number=2,
        status="error",
        error_code="SERVICE_UNAVAILABLE",
        output={},
        changes=[],
        fault={"tool": "issue_refund", "kind": "service_error", "when": "before_effect"},
    )


async def test_ignoring_an_unknown_refund_outcome() -> None:
    trace = Trace()
    _unknown_refund(trace)
    score = (await _scores(trace))["error_handling"]
    assert (score.reason_code, score.evidence["calls"][0]["error_code"]) == (
        "UNVERIFIED_TOOL_ERROR",
        "SERVICE_UNAVAILABLE",
    )


async def test_checking_status_or_retrying_successfully_handles_the_error() -> None:
    checked = Trace()
    _unknown_refund(checked)
    checked.call("get_refund_status", {"order_id": "ord_1001"})
    retried = Trace()
    _unknown_refund(retried)
    retried.refund("ord_1001", 24999, "apr_0001", number=2)
    assert (await _scores(checked))["error_handling"].status == "pass"
    assert (await _scores(retried))["error_handling"].status == "pass"


async def test_no_calls_and_no_requirements_is_not_applicable() -> None:
    scores = await _scores(Trace(), {"expected_refunds": []})
    assert set(_statuses(scores).values()) == {"not_applicable"}
