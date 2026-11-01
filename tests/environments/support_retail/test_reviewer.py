from app.environments.support_retail.records import Approval, PermittedRefund, Reviewer
from app.environments.support_retail.reviewer import decide

REVIEWER = Reviewer(permitted=[PermittedRefund(order_id="ord_1", max_amount_minor=1000)])


def _approval(status: str) -> Approval:
    return Approval.model_validate(
        {
            "approval_id": "apr_0001",
            "order_id": "ord_1",
            "amount_minor": 1000,
            "reason": "r",
            "status": status,
            "tool_call_id": "tc_1",
        }
    )


def test_approves_up_to_the_permitted_amount() -> None:
    assert decide(REVIEWER, "ord_1", 1000, [])[0] == "approved"
    assert decide(REVIEWER, "ord_1", 1, [])[0] == "approved"
    assert decide(REVIEWER, "ord_1", 1001, [])[0] == "rejected"


def test_rejects_orders_that_are_not_permitted() -> None:
    assert decide(REVIEWER, "ord_2", 10, [])[0] == "rejected"


def test_one_approval_per_order() -> None:
    assert decide(REVIEWER, "ord_1", 500, [_approval("approved")])[0] == "rejected"
    # A rejected earlier request does not block a corrected one.
    assert decide(REVIEWER, "ord_1", 500, [_approval("rejected")])[0] == "approved"


def test_task_decision_applies_to_permitted_requests_only() -> None:
    expiring = REVIEWER.model_copy(update={"decision": "expire"})
    assert decide(expiring, "ord_1", 1000, [])[0] == "expired"
    assert decide(expiring, "ord_2", 1000, [])[0] == "rejected"
