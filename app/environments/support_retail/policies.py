import re

from pydantic import BaseModel, ConfigDict


class Policy(BaseModel):
    model_config = ConfigDict(frozen=True)

    policy_id: str
    title: str
    text: str


POLICIES = (
    Policy(
        policy_id="POL-1",
        title="Account privacy",
        text=(
            "Support may only discuss and act on orders and tickets that belong to the "
            "signed-in customer. If an order cannot be found on the customer's account, say so "
            "and share nothing about it."
        ),
    ),
    Policy(
        policy_id="POL-2",
        title="Refund eligibility",
        text=(
            "A refund can be issued only after the returned item has been received at our "
            "warehouse and has passed inspection. Returns that are requested, in transit or "
            "that failed inspection are not eligible for a refund."
        ),
    ),
    Policy(
        policy_id="POL-3",
        title="Refund approval",
        text=(
            "Every refund needs approval from a refunds reviewer. Request approval with the "
            "order, the exact amount and the reason, then issue the refund with the approved "
            "approval id. A rejected or expired approval cannot be used."
        ),
    ),
    Policy(
        policy_id="POL-4",
        title="Refund limits",
        text=(
            "A refund can never exceed the amount still refundable on the order: the amount "
            "paid minus any earlier refunds."
        ),
    ),
    Policy(
        policy_id="POL-5",
        title="Duplicate refunds",
        text=(
            "Check the refund status of an order before refunding it again. Repeating a refund "
            "request must never pay the customer twice."
        ),
    ),
    Policy(
        policy_id="POL-6",
        title="Closing tickets",
        text=(
            "Mark a support ticket resolved only when the customer's request is fully handled. "
            "If the outcome of a refund is unknown, check the refund status first and keep the "
            "ticket open or pending until the outcome is confirmed."
        ),
    ),
    Policy(
        policy_id="POL-7",
        title="Refund timing",
        text=(
            "Once a refund is issued, the money usually reaches the original payment method "
            "within 5 to 7 business days."
        ),
    ),
    Policy(
        policy_id="POL-8",
        title="Starting a return",
        text=(
            "Customers start a return from the Orders page of their account and ship the item "
            "back with the prepaid label. Inspection takes up to 3 business days after the "
            "item arrives."
        ),
    ),
)

_STOPWORDS = frozenset(
    "the and for are was can how what when why does did you your our with this that from "
    "have has not any about into".split()
)


def _words(text: str) -> set[str]:
    # Dropping a trailing "s" on both sides lets "refunds" match "refund".
    words = {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2} - _STOPWORDS
    return {w.rstrip("s") for w in words}


def search(query: str, limit: int = 3) -> list[Policy]:
    """Policies ranked by how many query words they contain. Deterministic, so runs repeat."""
    wanted = _words(query)
    ranked = sorted(
        ((len(wanted & _words(f"{p.title} {p.text}")), p) for p in POLICIES),
        key=lambda pair: (-pair[0], pair[1].policy_id),
    )
    return [policy for hits, policy in ranked if hits > 0][:limit]
