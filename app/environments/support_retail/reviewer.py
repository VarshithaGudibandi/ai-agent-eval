from typing import Literal

from app.environments.support_retail.records import Approval, Reviewer

Decision = Literal["approved", "rejected", "expired"]

_DECISIONS: dict[str, Decision] = {
    "approve": "approved",
    "reject": "rejected",
    "expire": "expired",
}


def decide(
    reviewer: Reviewer, order_id: str, amount_minor: int, earlier: list[Approval]
) -> tuple[Decision, str]:
    """The simulated reviewer's answer to one approval request.

    It approves only an amount up to what the task permits for that order. A reviewer guards
    against paying too much, so a smaller amount than due can still be approved, and the
    evaluators must catch it. One order gets at most one approval, which keeps a second
    request from opening the door to a second refund.
    """
    if any(a.order_id == order_id and a.status == "approved" for a in earlier):
        return "rejected", "This order already has an approved refund request."
    permitted = next((p for p in reviewer.permitted if p.order_id == order_id), None)
    if permitted is None or amount_minor > permitted.max_amount_minor:
        return "rejected", "The reviewer did not approve this refund."
    decision = _DECISIONS[reviewer.decision]
    if decision == "expired":
        return decision, "No reviewer answered before the request expired. Nothing was approved."
    if decision == "rejected":
        return decision, "The reviewer did not approve this refund."
    return decision, "The reviewer approved this refund."
