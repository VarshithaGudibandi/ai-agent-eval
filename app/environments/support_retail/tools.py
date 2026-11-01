from typing import Literal

from pydantic import Field

from app.environments.contract import ToolArgs, ToolSchema

ORDER_ID = Field(min_length=1, max_length=100, description="The order id, such as ord_1001")


class GetOrderArgs(ToolArgs):
    order_id: str = ORDER_ID


class GetReturnStatusArgs(ToolArgs):
    order_id: str = ORDER_ID


class GetRefundStatusArgs(ToolArgs):
    order_id: str = ORDER_ID


class SearchPolicyArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=200, description="What to look up")


class RequestRefundApprovalArgs(ToolArgs):
    order_id: str = ORDER_ID
    amount_minor: int = Field(gt=0, description="Amount in minor units, such as cents or paise")
    reason: str = Field(min_length=1, max_length=500, description="Why the refund is due")


class IssueRefundArgs(ToolArgs):
    order_id: str = ORDER_ID
    amount_minor: int = Field(gt=0, description="Amount in minor units, such as cents or paise")
    approval_id: str = Field(
        min_length=1, max_length=100, description="An approved id from request_refund_approval"
    )


class UpdateTicketArgs(ToolArgs):
    ticket_id: str = Field(min_length=1, max_length=100, description="The support ticket id")
    status: Literal["open", "pending", "resolved"]
    note: str = Field(min_length=1, max_length=1000, description="What was done and why")


TOOLS = (
    ToolSchema(
        name="get_order",
        description="Look up one of the signed-in customer's orders: status, items and amounts.",
        args_model=GetOrderArgs,
    ),
    ToolSchema(
        name="get_return_status",
        description="Look up the return for an order: whether it arrived and passed inspection.",
        args_model=GetReturnStatusArgs,
    ),
    ToolSchema(
        name="get_refund_status",
        description="List the refunds already issued on an order and how much is still refundable.",
        args_model=GetRefundStatusArgs,
    ),
    ToolSchema(
        name="search_policy",
        description="Search the store's support policies.",
        args_model=SearchPolicyArgs,
    ),
    ToolSchema(
        name="request_refund_approval",
        description=(
            "Ask a refunds reviewer to approve a refund of an exact amount. Returns an approval "
            "id and the decision: approved, rejected or expired."
        ),
        args_model=RequestRefundApprovalArgs,
        effectful=True,
    ),
    ToolSchema(
        name="issue_refund",
        description=(
            "Refund an order to the original payment method. Needs an approved approval id "
            "for exactly this order and amount."
        ),
        args_model=IssueRefundArgs,
        effectful=True,
    ),
    ToolSchema(
        name="update_ticket",
        description="Set the status of the customer's support ticket and add a note.",
        args_model=UpdateTicketArgs,
        effectful=True,
    ),
)
