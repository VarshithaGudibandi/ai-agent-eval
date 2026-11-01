from typing import Any

from app.environments.contract import AgentTaskInput, TaskVersion
from app.evaluators.contract import EvalContext, Score, TraceEvent
from app.evaluators.outcome.refund_outcome import RefundOutcome


def _task(expected: list[dict[str, Any]] | None) -> TaskVersion:
    spec = {} if expected is None else {"expected_refunds": expected}
    return TaskVersion(
        id="task_1",
        task_key="t",
        environment="support-retail@1",
        scenario_family="refund",
        split="dev",
        agent_input=AgentTaskInput(user_message="m"),
        fixture={},
        evaluator_spec=spec,
    )


def _refund(n: int, order_id: str, amount: int) -> dict[str, Any]:
    return {
        "refund_id": f"ref_{n:04d}",
        "order_id": order_id,
        "amount_minor": amount,
        "currency": "USD",
        "reason": "r",
        "tool_call_id": f"tc_{n}",
    }


def _ctx(refunds: list[dict[str, Any]], expected: list[dict[str, Any]]) -> EvalContext:
    trace = [
        TraceEvent(
            id=f"evt_result_{r['tool_call_id']}",
            sequence_number=i + 1,
            event_type="tool_call_completed",
            payload={"tool_call_id": r["tool_call_id"]},
        )
        for i, r in enumerate(refunds)
    ]
    trace.append(
        TraceEvent(id="evt_final", sequence_number=99, event_type="agent_finished", payload={})
    )
    return EvalContext(
        task=_task(expected),
        final_snapshot={"refunds": refunds},
        trace=trace,
        final_response="",
    )


async def _scores(ctx: EvalContext) -> dict[str, Score]:
    return {s.dimension: s for s in await RefundOutcome().evaluate(ctx)}


EXPECTED = [{"order_id": "ord_1001", "amount_minor": 24999}]


async def test_exact_refund_passes_both_dimensions() -> None:
    scores = await _scores(_ctx([_refund(1, "ord_1001", 24999)], EXPECTED))
    assert {d: s.status for d, s in scores.items()} == {
        "refund_amount": "pass",
        "unexpected_refunds": "pass",
    }


async def test_wrong_amount_points_at_the_refund_call() -> None:
    score = (await _scores(_ctx([_refund(1, "ord_1001", 2499)], EXPECTED)))["refund_amount"]
    assert (score.status, score.reason_code) == ("fail", "WRONG_REFUND_AMOUNT")
    assert score.evidence == {
        "order_id": "ord_1001",
        "expected_amount_minor": 24999,
        "observed_amount_minor": 2499,
        "event_ids": ["evt_result_tc_1"],
    }


async def test_missing_refund_points_at_where_the_agent_stopped() -> None:
    score = (await _scores(_ctx([], EXPECTED)))["refund_amount"]
    assert score.reason_code == "MISSING_REFUND"
    assert score.evidence["event_ids"] == ["evt_final"]


async def test_two_refunds_on_one_order_is_a_duplicate() -> None:
    refunds = [_refund(1, "ord_1001", 12000), _refund(2, "ord_1001", 12999)]
    score = (await _scores(_ctx(refunds, EXPECTED)))["refund_amount"]
    assert score.reason_code == "DUPLICATE_REFUND"
    assert score.evidence["event_ids"] == ["evt_result_tc_1", "evt_result_tc_2"]


async def test_refund_on_another_order_is_unexpected() -> None:
    refunds = [_refund(1, "ord_1001", 24999), _refund(2, "ord_2002", 100)]
    scores = await _scores(_ctx(refunds, EXPECTED))
    assert scores["refund_amount"].status == "pass"
    unexpected = scores["unexpected_refunds"]
    assert (unexpected.status, unexpected.reason_code) == ("fail", "UNEXPECTED_REFUND")
    assert unexpected.evidence["event_ids"] == ["evt_result_tc_2"]


async def test_task_expecting_no_refund_fails_on_any_refund() -> None:
    scores = await _scores(_ctx([_refund(1, "ord_1001", 24999)], []))
    assert scores["refund_amount"].status == "not_applicable"
    assert scores["unexpected_refunds"].reason_code == "UNEXPECTED_REFUND"


def test_applies_only_to_tasks_with_expected_refunds() -> None:
    assert RefundOutcome().applies_to(_task(EXPECTED))
    assert RefundOutcome().applies_to(_task([]))
    assert not RefundOutcome().applies_to(_task(None))
