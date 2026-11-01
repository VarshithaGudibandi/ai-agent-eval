from app.evaluators.outcome.ticket_outcome import TicketOutcome
from tests.evaluators.traces import REFUND_SPEC, Trace, correct_refund, retail_task

SPEC = {**REFUND_SPEC, "expected_ticket": {"ticket_id": "tkt_1", "status_in": ["resolved"]}}


def _resolve(trace: Trace, status: str = "resolved") -> Trace:
    ticket = {
        "ticket_id": "tkt_1",
        "customer_id": "cust_12",
        "subject": "Refund",
        "status": status,
        "notes": ["done"],
    }
    change = {"collection": "tickets", "record_id": "tkt_1", "operation": "update", "data": ticket}
    trace.call(
        "update_ticket", {"ticket_id": "tkt_1", "status": status, "note": "done"}, changes=[change]
    )
    return trace


async def test_expected_status_passes() -> None:
    [score] = await TicketOutcome().evaluate(_resolve(correct_refund()).context(SPEC))
    assert score.status == "pass"


async def test_untouched_ticket_fails_at_the_end_of_the_run() -> None:
    ctx = correct_refund().context(SPEC)
    [score] = await TicketOutcome().evaluate(ctx)
    assert (score.reason_code, score.evidence["observed_status"]) == ("WRONG_TICKET_STATUS", "open")
    assert score.evidence["event_ids"] == [ctx.trace[-1].id]


async def test_wrong_status_points_at_the_update() -> None:
    ctx = _resolve(correct_refund(), "pending").context(SPEC)
    [score] = await TicketOutcome().evaluate(ctx)
    assert score.reason_code == "WRONG_TICKET_STATUS"
    assert score.evidence["event_ids"] == [ctx.tool_calls()[-1].event_id]


def test_applies_only_when_the_task_names_a_ticket() -> None:
    assert TicketOutcome().applies_to(retail_task(SPEC))
    assert not TicketOutcome().applies_to(retail_task(REFUND_SPEC))
