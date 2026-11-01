from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.environments.contract import (
    AgentTaskInput,
    CallContext,
    StateChange,
    TaskVersion,
    ToolArgs,
    ToolResult,
    ToolSchema,
)
from app.environments.state_store import EnvStateStore

CUSTOMERS = "customers"
ORDERS = "orders"
REFUNDS = "refunds"


class Customer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str
    name: str
    email: str


class OrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str
    name: str
    quantity: int = Field(gt=0)
    unit_price_minor: int = Field(ge=0)


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str
    customer_id: str
    status: Literal["placed", "shipped", "delivered", "cancelled"]
    currency: str
    total_minor: int = Field(ge=0)
    items: list[OrderItem]


class Refund(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refund_id: str
    order_id: str
    amount_minor: int
    currency: str
    reason: str
    tool_call_id: str


class RetailFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customers: list[Customer]
    orders: list[Order]


class GetOrderArgs(ToolArgs):
    order_id: str = Field(min_length=1, description="The order id, such as ord_1001")


class IssueRefundArgs(ToolArgs):
    order_id: str = Field(min_length=1, description="The order to refund")
    amount_minor: int = Field(gt=0, description="Amount in minor units, such as cents")
    reason: str = Field(min_length=1, max_length=500, description="Why the refund is issued")


TOOLS = [
    ToolSchema(
        name="get_order",
        description="Look up one of the signed-in customer's orders, including refunds so far.",
        args_model=GetOrderArgs,
    ),
    ToolSchema(
        name="issue_refund",
        description="Refund part or all of an order to the original payment method.",
        args_model=IssueRefundArgs,
    ),
]

# Not-found and not-yours give the same answer, so an agent cannot probe for other
# customers' order ids.
ORDER_NOT_FOUND = ToolResult.error("ORDER_NOT_FOUND", "No such order for this customer.")


class SupportRetail:
    name = "support-retail"
    version = "1"

    def __init__(self, store: EnvStateStore) -> None:
        self._store = store

    def tool_schemas(self) -> list[ToolSchema]:
        return list(TOOLS)

    def agent_view(self, task: TaskVersion) -> AgentTaskInput:
        return task.agent_input

    async def setup(self, env_id: str, fixture: dict[str, Any]) -> None:
        world = RetailFixture.model_validate(fixture)
        for customer in world.customers:
            await self._store.put(env_id, CUSTOMERS, customer.customer_id, customer.model_dump())
        for order in world.orders:
            await self._store.put(env_id, ORDERS, order.order_id, order.model_dump())

    async def execute(self, env_id: str, tool: str, args: ToolArgs, ctx: CallContext) -> ToolResult:
        customer_id = ctx.task_context.get("authenticated_customer_id")
        if not isinstance(customer_id, str):
            return ToolResult.error("NOT_AUTHENTICATED", "No signed-in customer.")
        match args:
            case GetOrderArgs():
                return await self._get_order(env_id, customer_id, args)
            case IssueRefundArgs():
                return await self._issue_refund(env_id, customer_id, args, ctx)
        raise ValueError(f"support-retail has no tool {tool!r}")

    async def snapshot(self, env_id: str) -> dict[str, Any]:
        state = await self._store.snapshot(env_id)
        return {name: state.get(name, []) for name in (CUSTOMERS, ORDERS, REFUNDS)}

    async def teardown(self, env_id: str) -> None:
        await self._store.clear(env_id)

    async def _owned_order(
        self, env_id: str, customer_id: str, order_id: str, *, for_update: bool = False
    ) -> Order | None:
        data = await self._store.get(env_id, ORDERS, order_id, for_update=for_update)
        if data is None:
            return None
        order = Order.model_validate(data)
        return order if order.customer_id == customer_id else None

    async def _refunds_for(self, env_id: str, order_id: str) -> list[Refund]:
        refunds = [
            Refund.model_validate(r) for r in await self._store.list_records(env_id, REFUNDS)
        ]
        return [r for r in refunds if r.order_id == order_id]

    async def _get_order(self, env_id: str, customer_id: str, args: GetOrderArgs) -> ToolResult:
        order = await self._owned_order(env_id, customer_id, args.order_id)
        if order is None:
            return ORDER_NOT_FOUND
        refunded = sum(r.amount_minor for r in await self._refunds_for(env_id, order.order_id))
        return ToolResult.ok(
            {**order.model_dump(exclude={"customer_id"}), "refunded_minor": refunded}
        )

    async def _issue_refund(
        self, env_id: str, customer_id: str, args: IssueRefundArgs, ctx: CallContext
    ) -> ToolResult:
        # Locking the order serializes refunds on it, so two concurrent calls cannot
        # both pass the balance check.
        order = await self._owned_order(env_id, customer_id, args.order_id, for_update=True)
        if order is None:
            return ORDER_NOT_FOUND
        refunded = sum(r.amount_minor for r in await self._refunds_for(env_id, order.order_id))
        remaining = order.total_minor - refunded
        if args.amount_minor > remaining:
            return ToolResult.error(
                "REFUND_EXCEEDS_BALANCE",
                f"Only {remaining} {order.currency} minor units are left to refund.",
            )
        count = len(await self._store.list_records(env_id, REFUNDS))
        refund = Refund(
            refund_id=f"ref_{count + 1:04d}",
            order_id=order.order_id,
            amount_minor=args.amount_minor,
            currency=order.currency,
            reason=args.reason,
            tool_call_id=ctx.tool_call_id,
        )
        data = refund.model_dump()
        await self._store.put(env_id, REFUNDS, refund.refund_id, data)
        return ToolResult.ok(
            {
                "refund_id": refund.refund_id,
                "amount_minor": refund.amount_minor,
                "currency": refund.currency,
            },
            [
                StateChange(
                    collection=REFUNDS, record_id=refund.refund_id, operation="insert", data=data
                )
            ],
        )
