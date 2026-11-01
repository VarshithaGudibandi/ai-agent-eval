from app.environments.contract import AgentTaskInput, TaskVersion
from app.evaluators.outcome.refund_outcome import RefundOutcome
from tests.evaluators.traces import REFUND_SPEC, Trace, by_dimension, correct_refund


async def test_correct_refund_passes() -> None:
    scores = by_dimension(await RefundOutcome().evaluate(correct_refund().context(REFUND_SPEC)))
    assert {d: s.status for d, s in scores.items()} == {
        "refund_amount": "pass",
        "unexpected_refunds": "pass",
    }


async def test_wrong_amount_fails_and_points_at_the_refund() -> None:
    trace = Trace()
    approval = trace.approve("ord_1001", 2499)
    trace.refund("ord_1001", 2499, approval, number=2)
    ctx = trace.context(REFUND_SPEC)
    score = by_dimension(await RefundOutcome().evaluate(ctx))["refund_amount"]
    assert (score.status, score.reason_code) == ("fail", "WRONG_REFUND_AMOUNT")
    assert score.evidence["observed_amount_minor"] == 2499
    refund_event = ctx.tool_result_event("tc_2")
    assert refund_event is not None
    assert score.evidence["event_ids"] == [refund_event.id]


async def test_missing_refund_points_at_where_the_agent_stopped() -> None:
    ctx = Trace().context(REFUND_SPEC)
    score = by_dimension(await RefundOutcome().evaluate(ctx))["refund_amount"]
    assert score.reason_code == "MISSING_REFUND"
    assert score.evidence["event_ids"] == [ctx.trace[-1].id]


async def test_two_refunds_for_one_expected_are_a_duplicate() -> None:
    trace = correct_refund()
    trace.refund("ord_1001", 24999, "apr_0001", number=3)
    score = by_dimension(await RefundOutcome().evaluate(trace.context(REFUND_SPEC)))
    assert score["refund_amount"].reason_code == "DUPLICATE_REFUND"
    assert len(score["refund_amount"].evidence["event_ids"]) == 2


async def test_refund_nobody_expected_is_flagged() -> None:
    trace = correct_refund()
    trace.refund("ord_1002", 1000, "apr_0002", number=3)
    score = by_dimension(await RefundOutcome().evaluate(trace.context(REFUND_SPEC)))
    assert score["refund_amount"].status == "pass"
    assert score["unexpected_refunds"].reason_code == "UNEXPECTED_REFUND"


async def test_refunds_from_before_the_attempt_are_not_the_agents() -> None:
    # The fixture already holds a refund on ord_1002.
    scores = by_dimension(await RefundOutcome().evaluate(Trace().context({"expected_refunds": []})))
    assert scores["refund_amount"].status == "not_applicable"
    assert scores["unexpected_refunds"].status == "pass"


def test_applies_only_to_the_retail_store() -> None:
    task = TaskVersion(
        id="t",
        task_key="t",
        environment="it-support@1",
        scenario_family="f",
        split="dev",
        agent_input=AgentTaskInput(user_message="m"),
        fixture={},
        evaluator_spec={},
    )
    assert not RefundOutcome().applies_to(task)
