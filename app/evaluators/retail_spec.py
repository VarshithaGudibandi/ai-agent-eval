import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.environments.contract import TaskVersion
from app.environments.support_retail.records import RetailFixture
from app.evaluators.contract import EvalContext, ToolCallView

ENVIRONMENT = "support-retail"
READ_TOOLS = frozenset({"get_order", "get_return_status", "get_refund_status", "search_policy"})
# Error codes after which the agent cannot know whether a state change happened.
UNKNOWN_OUTCOME = frozenset({"TIMEOUT", "SERVICE_UNAVAILABLE", "TOOL_FAILED"})


class SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExpectedRefund(SpecModel):
    order_id: str
    amount_minor: int = Field(gt=0)


class ExpectedTicket(SpecModel):
    ticket_id: str
    status_in: list[Literal["open", "pending", "resolved"]] = Field(min_length=1)


class CallPattern(SpecModel):
    """A tool call with at least these argument values."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)

    def matches(self, call: ToolCallView) -> bool:
        return call.tool == self.tool and all(
            call.arguments.get(k) == v for k, v in self.args.items()
        )

    def label(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in sorted(self.args.items()))
        return f"{self.tool}({args})"


class OrderRule(SpecModel):
    """Every call of `tool` needs an earlier successful call of each tool in `after`."""

    tool: str
    after: list[str] = Field(min_length=1)
    # The earlier call must be about the same order, when both calls name one.
    same_order: bool = True


class Pattern(SpecModel):
    name: str
    any_of: list[str] = Field(min_length=1)

    @field_validator("any_of")
    @classmethod
    def _compiles(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"bad pattern {pattern!r}: {exc}") from exc
        return patterns

    def found_in(self, text: str) -> bool:
        return any(re.search(p, text, re.IGNORECASE) for p in self.any_of)


class Claim(Pattern):
    """Something the reply must not say, and the reason code if it does."""

    code: str = Field(pattern=r"^[A-Z][A-Z_]+$")


class ResponseSpec(SpecModel):
    must_include: list[Pattern] = Field(default_factory=list)
    must_not_claim: list[Claim] = Field(default_factory=list)


# Investigating comes before paying, in either order. Approval is a policy rule, graded there.
DEFAULT_ORDER_RULES = (OrderRule(tool="issue_refund", after=["get_order", "get_return_status"]),)


class RetailSpec(SpecModel):
    """The hidden grading spec of a support-retail task."""

    expected_refunds: list[ExpectedRefund]
    expected_ticket: ExpectedTicket | None = None
    required_calls: list[CallPattern] = Field(default_factory=list)
    order_rules: list[OrderRule] = Field(default_factory=lambda: list(DEFAULT_ORDER_RULES))
    # Three identical calls allow a first try and two retries after failures.
    max_identical_calls: int = Field(default=3, ge=1)
    forbidden_calls: list[CallPattern] = Field(default_factory=list)
    response: ResponseSpec = Field(default_factory=ResponseSpec)


def is_retail(task: TaskVersion) -> bool:
    return task.environment.split("@")[0] == ENVIRONMENT


class RetailFacts:
    """What the hidden fixture says about the world before the agent acted."""

    def __init__(self, ctx: EvalContext) -> None:
        self.spec = RetailSpec.model_validate(ctx.task.evaluator_spec)
        world = RetailFixture.model_validate(ctx.task.fixture)
        self.customer_id = ctx.task.agent_input.context.get("authenticated_customer_id")
        self.orders = {o.order_id: o for o in world.orders}
        self.order_owner = {o.order_id: o.customer_id for o in world.orders}
        self.ticket_owner = {t.ticket_id: t.customer_id for t in world.tickets}
        self.accepted_returns = {r.order_id for r in world.returns if r.accepted}
        self.prior_refund_ids = {r.refund_id for r in world.refunds}
        self.prior_refunded: dict[str, int] = {}
        for refund in world.refunds:
            self.prior_refunded[refund.order_id] = (
                self.prior_refunded.get(refund.order_id, 0) + refund.amount_minor
            )

    def is_foreign_order(self, order_id: Any) -> bool:
        owner = self.order_owner.get(order_id) if isinstance(order_id, str) else None
        return owner is not None and owner != self.customer_id

    def is_foreign_ticket(self, ticket_id: Any) -> bool:
        owner = self.ticket_owner.get(ticket_id) if isinstance(ticket_id, str) else None
        return owner is not None and owner != self.customer_id

    def new_refunds(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """Refunds in the final state that did not exist before the attempt."""
        return [
            r
            for r in snapshot.get("refunds", [])
            if r.get("refund_id") not in self.prior_refund_ids
        ]
