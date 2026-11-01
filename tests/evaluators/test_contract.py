from tests.evaluators.traces import Trace, correct_refund


def test_tool_calls_pair_requests_with_results() -> None:
    ctx = correct_refund().context({"expected_refunds": []})
    calls = ctx.tool_calls()
    assert [c.tool for c in calls] == [
        "get_order",
        "get_return_status",
        "request_refund_approval",
        "issue_refund",
    ]
    refund = calls[-1]
    assert refund.status == "ok"
    assert refund.event_id == refund.result_event_id
    assert [r["refund_id"] for r in refund.inserted("refunds")] == ["ref_0002"]
    assert refund.inserted("approvals") == []


def test_fault_and_replay_are_carried_over() -> None:
    trace = Trace()
    fault = {"tool": "get_order", "kind": "timeout", "when": "before_effect"}
    trace.call(
        "get_order", {"order_id": "ord_1001"}, status="error", error_code="TIMEOUT", fault=fault
    )
    trace.call("update_ticket", {"ticket_id": "tkt_1"}, replayed=True)
    first, second = trace.context({"expected_refunds": []}).tool_calls()
    assert (first.fault, first.error_code) == (fault, "TIMEOUT")
    assert second.replayed


def test_unfinished_call_points_at_its_request() -> None:
    trace = Trace()
    trace.call("get_order", {"order_id": "ord_1001"})
    del trace.events[-1]
    [call] = trace.context({"expected_refunds": []}).tool_calls()
    assert (call.result_event_id, call.event_id) == (None, call.request_event_id)


def test_finish_event_is_where_the_agent_stopped() -> None:
    ctx = Trace().context({"expected_refunds": []})
    assert ctx.finish_event_id() == ctx.trace[-1].id
