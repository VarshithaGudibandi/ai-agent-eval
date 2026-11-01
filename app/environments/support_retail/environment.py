from typing import Any

from pydantic import BaseModel

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
from app.environments.support_retail import policies, reviewer
from app.environments.support_retail.records import (
    APPROVALS,
    CUSTOMERS,
    ORDERS,
    REFUNDS,
    RETURNS,
    REVIEWER,
    SNAPSHOT_COLLECTIONS,
    TICKETS,
    Approval,
    Order,
    Refund,
    RetailFixture,
    Return,
    Reviewer,
    Ticket,
)
from app.environments.support_retail.tools import (
    TOOLS,
    GetOrderArgs,
    GetRefundStatusArgs,
    GetReturnStatusArgs,
    IssueRefundArgs,
    RequestRefundApprovalArgs,
    SearchPolicyArgs,
    UpdateTicketArgs,
)

# Not-found and not-yours give the same answer, so an agent cannot probe for other
# customers' order ids.
ORDER_NOT_FOUND = ToolResult.error("ORDER_NOT_FOUND", "No such order for this customer.")
TICKET_NOT_FOUND = ToolResult.error("TICKET_NOT_FOUND", "No such ticket for this customer.")


def _change(collection: str, record_id: str, operation: str, record: BaseModel) -> StateChange:
    return StateChange.model_validate(
        {
            "collection": collection,
            "record_id": record_id,
            "operation": operation,
            "data": record.model_dump(),
        }
    )


class SupportRetail:
    """A fictional electronics retailer: orders, returns, refunds, approvals and tickets.

    The environment enforces what a real back office could know: ownership, approvals,
    refund balances and one approval per order. Whether a ticket was closed while a refund's
    outcome was still unknown depends on what the agent saw, so only the evaluators judge it.
    """

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
        records: list[tuple[str, str, BaseModel]] = [
            *((CUSTOMERS, c.customer_id, c) for c in world.customers),
            *((ORDERS, o.order_id, o) for o in world.orders),
            *((RETURNS, r.order_id, r) for r in world.returns),
            *((REFUNDS, r.refund_id, r) for r in world.refunds),
            *((TICKETS, t.ticket_id, t) for t in world.tickets),
            (REVIEWER, "reviewer", world.reviewer),
        ]
        for collection, record_id, record in records:
            await self._store.put(env_id, collection, record_id, record.model_dump())

    async def execute(self, env_id: str, tool: str, args: ToolArgs, ctx: CallContext) -> ToolResult:
        customer_id = ctx.task_context.get("authenticated_customer_id")
        if not isinstance(customer_id, str):
            return ToolResult.error("NOT_AUTHENTICATED", "No signed-in customer.")
        match args:
            case GetOrderArgs():
                return await self._get_order(env_id, customer_id, args)
            case GetReturnStatusArgs():
                return await self._get_return_status(env_id, customer_id, args)
            case GetRefundStatusArgs():
                return await self._get_refund_status(env_id, customer_id, args)
            case SearchPolicyArgs():
                return ToolResult.ok(
                    {"results": [p.model_dump() for p in policies.search(args.query)]}
                )
            case RequestRefundApprovalArgs():
                return await self._request_approval(env_id, customer_id, args, ctx)
            case IssueRefundArgs():
                return await self._issue_refund(env_id, customer_id, args, ctx)
            case UpdateTicketArgs():
                return await self._update_ticket(env_id, customer_id, args)
        raise ValueError(f"support-retail has no tool {tool!r}")

    async def snapshot(self, env_id: str) -> dict[str, Any]:
        state = await self._store.snapshot(env_id)
        return {name: state.get(name, []) for name in SNAPSHOT_COLLECTIONS}

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

    async def _next_id(self, env_id: str, collection: str, field: str, prefix: str) -> str:
        # Ids are sequential per attempt, so a scripted agent can know them in advance.
        taken = {r[field] for r in await self._store.list_records(env_id, collection)}
        number = len(taken) + 1
        while f"{prefix}_{number:04d}" in taken:
            number += 1
        return f"{prefix}_{number:04d}"

    async def _get_order(self, env_id: str, customer_id: str, args: GetOrderArgs) -> ToolResult:
        order = await self._owned_order(env_id, customer_id, args.order_id)
        if order is None:
            return ORDER_NOT_FOUND
        refunded = sum(r.amount_minor for r in await self._refunds_for(env_id, order.order_id))
        return ToolResult.ok(
            {**order.model_dump(exclude={"customer_id"}), "refunded_minor": refunded}
        )

    async def _get_return_status(
        self, env_id: str, customer_id: str, args: GetReturnStatusArgs
    ) -> ToolResult:
        order = await self._owned_order(env_id, customer_id, args.order_id)
        if order is None:
            return ORDER_NOT_FOUND
        data = await self._store.get(env_id, RETURNS, order.order_id)
        if data is None:
            return ToolResult.ok({"order_id": order.order_id, "status": "not_started"})
        return ToolResult.ok(Return.model_validate(data).model_dump())

    async def _get_refund_status(
        self, env_id: str, customer_id: str, args: GetRefundStatusArgs
    ) -> ToolResult:
        order = await self._owned_order(env_id, customer_id, args.order_id)
        if order is None:
            return ORDER_NOT_FOUND
        refunds = await self._refunds_for(env_id, order.order_id)
        refunded = sum(r.amount_minor for r in refunds)
        return ToolResult.ok(
            {
                "order_id": order.order_id,
                "currency": order.currency,
                "total_minor": order.total_minor,
                "refunded_minor": refunded,
                "refundable_minor": order.total_minor - refunded,
                "refunds": [
                    r.model_dump(include={"refund_id", "amount_minor", "issued_on"})
                    for r in refunds
                ],
            }
        )

    async def _request_approval(
        self, env_id: str, customer_id: str, args: RequestRefundApprovalArgs, ctx: CallContext
    ) -> ToolResult:
        order = await self._owned_order(env_id, customer_id, args.order_id, for_update=True)
        if order is None:
            return ORDER_NOT_FOUND
        config = Reviewer.model_validate(await self._store.get(env_id, REVIEWER, "reviewer"))
        earlier = [
            Approval.model_validate(a) for a in await self._store.list_records(env_id, APPROVALS)
        ]
        status, message = reviewer.decide(config, order.order_id, args.amount_minor, earlier)
        approval = Approval(
            approval_id=await self._next_id(env_id, APPROVALS, "approval_id", "apr"),
            order_id=order.order_id,
            amount_minor=args.amount_minor,
            reason=args.reason,
            status=status,
            tool_call_id=ctx.tool_call_id,
        )
        await self._store.put(env_id, APPROVALS, approval.approval_id, approval.model_dump())
        return ToolResult.ok(
            {
                "approval_id": approval.approval_id,
                "order_id": order.order_id,
                "amount_minor": args.amount_minor,
                "status": status,
                "message": message,
            },
            [_change(APPROVALS, approval.approval_id, "insert", approval)],
        )

    async def _issue_refund(
        self, env_id: str, customer_id: str, args: IssueRefundArgs, ctx: CallContext
    ) -> ToolResult:
        # Locking the order serializes refunds on it, so two concurrent calls cannot
        # both pass the balance check.
        order = await self._owned_order(env_id, customer_id, args.order_id, for_update=True)
        if order is None:
            return ORDER_NOT_FOUND
        data = await self._store.get(env_id, APPROVALS, args.approval_id, for_update=True)
        approval = Approval.model_validate(data) if data is not None else None
        if approval is None or approval.order_id != order.order_id:
            return ToolResult.error("APPROVAL_NOT_FOUND", "No such approval for this order.")
        if approval.status != "approved":
            return ToolResult.error("APPROVAL_NOT_VALID", f"The approval was {approval.status}.")
        if approval.refund_id is not None:
            return ToolResult.error(
                "APPROVAL_ALREADY_USED", f"The approval was used by {approval.refund_id}."
            )
        if args.amount_minor != approval.amount_minor:
            return ToolResult.error(
                "APPROVAL_MISMATCH",
                f"The approval covers {approval.amount_minor} minor units, not "
                f"{args.amount_minor}.",
            )
        refunded = sum(r.amount_minor for r in await self._refunds_for(env_id, order.order_id))
        remaining = order.total_minor - refunded
        if args.amount_minor > remaining:
            return ToolResult.error(
                "REFUND_EXCEEDS_BALANCE",
                f"Only {remaining} {order.currency} minor units are left to refund.",
            )
        refund = Refund(
            refund_id=await self._next_id(env_id, REFUNDS, "refund_id", "ref"),
            order_id=order.order_id,
            amount_minor=args.amount_minor,
            currency=order.currency,
            approval_id=approval.approval_id,
            tool_call_id=ctx.tool_call_id,
        )
        approval.refund_id = refund.refund_id
        await self._store.put(env_id, REFUNDS, refund.refund_id, refund.model_dump())
        await self._store.put(env_id, APPROVALS, approval.approval_id, approval.model_dump())
        return ToolResult.ok(
            {
                "refund_id": refund.refund_id,
                "order_id": order.order_id,
                "amount_minor": refund.amount_minor,
                "currency": refund.currency,
            },
            [
                _change(REFUNDS, refund.refund_id, "insert", refund),
                _change(APPROVALS, approval.approval_id, "update", approval),
            ],
        )

    async def _update_ticket(
        self, env_id: str, customer_id: str, args: UpdateTicketArgs
    ) -> ToolResult:
        data = await self._store.get(env_id, TICKETS, args.ticket_id, for_update=True)
        ticket = Ticket.model_validate(data) if data is not None else None
        if ticket is None or ticket.customer_id != customer_id:
            return TICKET_NOT_FOUND
        ticket.status = args.status
        ticket.notes.append(args.note)
        await self._store.put(env_id, TICKETS, ticket.ticket_id, ticket.model_dump())
        return ToolResult.ok(
            ticket.model_dump(exclude={"customer_id"}),
            [_change(TICKETS, ticket.ticket_id, "update", ticket)],
        )
