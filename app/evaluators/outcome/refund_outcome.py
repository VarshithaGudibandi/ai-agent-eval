from collections import defaultdict
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.environments.contract import TaskVersion
from app.evaluators.contract import EvalContext, Layer, Score


class ExpectedRefund(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str
    amount_minor: int = Field(gt=0)


class RefundSpec(BaseModel):
    expected_refunds: list[ExpectedRefund]


class ObservedRefund(BaseModel):
    model_config = ConfigDict(extra="ignore")

    refund_id: str
    order_id: str
    amount_minor: int
    tool_call_id: str


class RefundOutcome:
    """Compares the refunds in the final state with the refunds the task expects."""

    name = "refund_outcome"
    version = "1.0.0"
    layer: Layer = "outcome"

    def applies_to(self, task: TaskVersion) -> bool:
        return "expected_refunds" in task.evaluator_spec

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        spec = RefundSpec.model_validate(ctx.task.evaluator_spec)
        observed = [ObservedRefund.model_validate(r) for r in ctx.final_snapshot["refunds"]]
        by_order: dict[str, list[ObservedRefund]] = defaultdict(list)
        for refund in observed:
            by_order[refund.order_id].append(refund)
        expected_orders = {e.order_id for e in spec.expected_refunds}
        unexpected = [r for r in observed if r.order_id not in expected_orders]
        return [
            self._amount_score(ctx, spec.expected_refunds, by_order),
            self._unexpected_score(ctx, unexpected),
        ]

    def _amount_score(
        self,
        ctx: EvalContext,
        expected: list[ExpectedRefund],
        by_order: dict[str, list[ObservedRefund]],
    ) -> Score:
        if not expected:
            return self._score("refund_amount", applicable=False, status="not_applicable")
        for want in expected:
            got = by_order.get(want.order_id, [])
            if not got:
                return self._fail(
                    "refund_amount",
                    "MISSING_REFUND",
                    {"order_id": want.order_id, "expected_amount_minor": want.amount_minor},
                    ctx,
                    [],
                )
            if len(got) > 1:
                return self._fail(
                    "refund_amount",
                    "DUPLICATE_REFUND",
                    {
                        "order_id": want.order_id,
                        "expected_amount_minor": want.amount_minor,
                        "observed_amounts_minor": [r.amount_minor for r in got],
                    },
                    ctx,
                    got,
                )
            if got[0].amount_minor != want.amount_minor:
                return self._fail(
                    "refund_amount",
                    "WRONG_REFUND_AMOUNT",
                    {
                        "order_id": want.order_id,
                        "expected_amount_minor": want.amount_minor,
                        "observed_amount_minor": got[0].amount_minor,
                    },
                    ctx,
                    got,
                )
        return self._score("refund_amount", applicable=True, status="pass", score=1.0)

    def _unexpected_score(self, ctx: EvalContext, unexpected: list[ObservedRefund]) -> Score:
        if not unexpected:
            return self._score("unexpected_refunds", applicable=True, status="pass", score=1.0)
        return self._fail(
            "unexpected_refunds",
            "UNEXPECTED_REFUND",
            {"refunds": [r.model_dump(exclude={"tool_call_id"}) for r in unexpected]},
            ctx,
            unexpected,
        )

    def _fail(
        self,
        dimension: str,
        reason: str,
        details: dict[str, Any],
        ctx: EvalContext,
        refunds: list[ObservedRefund],
    ) -> Score:
        found = [ctx.tool_result_event(r.tool_call_id) for r in refunds]
        event_ids = [e.id for e in found if e is not None]
        if not refunds:
            # Nothing happened to point at, so point at where the agent stopped.
            event_ids = [e.id for e in ctx.trace if e.event_type == "agent_finished"][-1:]
        evidence = {**details, "event_ids": event_ids}
        return self._score(
            dimension, applicable=True, status="fail", score=0.0, reason=reason, evidence=evidence
        )

    def _score(
        self,
        dimension: str,
        *,
        applicable: bool,
        status: str,
        score: float | None = None,
        reason: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> Score:
        return Score.model_validate(
            {
                "evaluator": self.name,
                "version": self.version,
                "layer": self.layer,
                "dimension": dimension,
                "applicable": applicable,
                "status": status,
                "score": score,
                "reason_code": reason,
                "evidence": evidence or {},
            }
        )
