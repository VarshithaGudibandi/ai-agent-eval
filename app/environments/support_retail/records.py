from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CUSTOMERS = "customers"
ORDERS = "orders"
RETURNS = "returns"
REFUNDS = "refunds"
APPROVALS = "approvals"
TICKETS = "tickets"
REVIEWER = "reviewer"

# The collections a snapshot shows. The reviewer's instructions are hidden setup, not state.
SNAPSHOT_COLLECTIONS = (CUSTOMERS, ORDERS, RETURNS, REFUNDS, APPROVALS, TICKETS)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Customer(Record):
    customer_id: str
    name: str
    email: str


class OrderItem(Record):
    sku: str
    name: str
    quantity: int = Field(gt=0)
    unit_price_minor: int = Field(ge=0)


class Order(Record):
    order_id: str
    customer_id: str
    status: Literal["placed", "shipped", "delivered", "cancelled"]
    currency: str
    total_minor: int = Field(ge=0)
    items: list[OrderItem]
    # Free text the customer wrote. It is untrusted and may carry injected instructions.
    note: str = ""


class Return(Record):
    order_id: str
    status: Literal["requested", "in_transit", "received"]
    inspection: Literal["pending", "passed", "failed"] = "pending"
    received_on: str | None = None
    customer_comment: str = ""

    @property
    def accepted(self) -> bool:
        return self.status == "received" and self.inspection == "passed"


class Refund(Record):
    refund_id: str
    order_id: str
    amount_minor: int = Field(gt=0)
    currency: str
    approval_id: str | None = None
    issued_on: str | None = None
    tool_call_id: str | None = None


class Approval(Record):
    approval_id: str
    order_id: str
    amount_minor: int
    reason: str
    status: Literal["approved", "rejected", "expired"]
    refund_id: str | None = None
    tool_call_id: str
    # Decisions come from the reviewer simulator, never a person, and reports say so.
    simulated: bool = True


class Ticket(Record):
    ticket_id: str
    customer_id: str
    subject: str
    status: Literal["open", "pending", "resolved"]
    notes: list[str] = Field(default_factory=list)


class PermittedRefund(Record):
    order_id: str
    max_amount_minor: int = Field(gt=0)


class Reviewer(Record):
    """How the simulated refunds reviewer answers approval requests in this task."""

    decision: Literal["approve", "reject", "expire"] = "approve"
    permitted: list[PermittedRefund] = Field(default_factory=list)


class RetailFixture(Record):
    customers: list[Customer]
    orders: list[Order]
    returns: list[Return] = Field(default_factory=list)
    refunds: list[Refund] = Field(default_factory=list)
    tickets: list[Ticket] = Field(default_factory=list)
    reviewer: Reviewer = Field(default_factory=Reviewer)
