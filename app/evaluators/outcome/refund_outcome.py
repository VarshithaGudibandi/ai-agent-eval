from collections import defaultdict
from typing import Any

from app.environments.contract import TaskVersion
from app.evaluators.contract import EvalContext, Layer, Score
from app.evaluators.retail_spec import ExpectedRefund, RetailFacts, is_retail
from app.evaluators.scores import not_applicable, verdict


class RefundOutcome:
    """Compares the refunds the attempt created with the refunds the task expects."""

    name = "refund_outcome"
    version = "1.1.0"
    layer: Layer = "outcome"

    def applies_to(self, task: TaskVersion) -> bool:
        return is_retail(task)

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        facts = RetailFacts(ctx)
        created = facts.new_refunds(ctx.final_snapshot)
        by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for refund in created:
            by_order[refund["order_id"]].append(refund)
        expected_orders = {e.order_id for e in facts.spec.expected_refunds}
        unexpected = [r for r in created if r["order_id"] not in expected_orders]
        return [
            self._amount_score(ctx, facts.spec.expected_refunds, by_order),
            self._unexpected_score(ctx, unexpected),
        ]

    def _amount_score(
        self,
        ctx: EvalContext,
        expected: list[ExpectedRefund],
        by_order: dict[str, list[dict[str, Any]]],
    ) -> Score:
        if not expected:
            return not_applicable(self, "refund_amount")
        for want in expected:
            got = by_order.get(want.order_id, [])
            details: dict[str, Any] = {
                "order_id": want.order_id,
                "expected_amount_minor": want.amount_minor,
            }
            if not got:
                return self._fail(ctx, "refund_amount", "MISSING_REFUND", details, [])
            if len(got) > 1:
                details["observed_amounts_minor"] = [r["amount_minor"] for r in got]
                return self._fail(ctx, "refund_amount", "DUPLICATE_REFUND", details, got)
            if got[0]["amount_minor"] != want.amount_minor:
                details["observed_amount_minor"] = got[0]["amount_minor"]
                return self._fail(ctx, "refund_amount", "WRONG_REFUND_AMOUNT", details, got)
        return verdict(self, "refund_amount", "pass")

    def _unexpected_score(self, ctx: EvalContext, unexpected: list[dict[str, Any]]) -> Score:
        if not unexpected:
            return verdict(self, "unexpected_refunds", "pass")
        details = {
            "refunds": [
                {k: r.get(k) for k in ("refund_id", "order_id", "amount_minor")} for r in unexpected
            ]
        }
        return self._fail(ctx, "unexpected_refunds", "UNEXPECTED_REFUND", details, unexpected)

    def _fail(
        self,
        ctx: EvalContext,
        dimension: str,
        reason: str,
        details: dict[str, Any],
        refunds: list[dict[str, Any]],
    ) -> Score:
        found = [ctx.tool_result_event(r.get("tool_call_id") or "") for r in refunds]
        event_ids = [e.id for e in found if e is not None]
        if not refunds:
            # Nothing happened to point at, so point at where the agent stopped.
            event_ids = [i for i in [ctx.finish_event_id()] if i]
        return verdict(
            self, dimension, "fail", reason=reason, evidence={**details, "event_ids": event_ids}
        )
