from app.environments.contract import TaskVersion
from app.evaluators.contract import EvalContext, Layer, Score, ToolCallView
from app.evaluators.retail_spec import UNKNOWN_OUTCOME, RetailFacts, is_retail
from app.evaluators.scores import not_applicable, verdict
from app.evaluators.trajectory.checks import call_order, repetition, required_calls


class RetailTrajectory:
    """Grades how the agent got there: investigation, order, repetition and error handling."""

    name = "retail_trajectory"
    version = "1.0.0"
    layer: Layer = "trajectory"

    def applies_to(self, task: TaskVersion) -> bool:
        return is_retail(task)

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        spec = RetailFacts(ctx).spec
        calls = ctx.tool_calls()
        return [
            required_calls(self, ctx, spec.required_calls, calls),
            call_order(self, spec.order_rules, calls),
            repetition(self, spec.max_identical_calls, calls),
            self._error_handling(calls),
        ]

    def _error_handling(self, calls: list[ToolCallView]) -> Score:
        # A refund that failed with an unknown outcome may or may not have paid. The agent
        # must find out, by checking the refund status or by a retry that succeeds.
        unknown = [
            (index, call)
            for index, call in enumerate(calls)
            if call.tool == "issue_refund"
            and call.status == "error"
            and call.error_code in UNKNOWN_OUTCOME
        ]
        if not unknown:
            return not_applicable(self, "error_handling")
        unverified = []
        for index, call in unknown:
            order_id = call.arguments.get("order_id")
            verified = any(
                c.status == "ok"
                and c.arguments.get("order_id") == order_id
                and c.tool in ("get_refund_status", "issue_refund")
                for c in calls[index + 1 :]
            )
            if not verified:
                unverified.append(call)
        if not unverified:
            return verdict(self, "error_handling", "pass")
        return verdict(
            self,
            "error_handling",
            "fail",
            reason="UNVERIFIED_TOOL_ERROR",
            evidence={
                "calls": [
                    {"order_id": c.arguments.get("order_id"), "error_code": c.error_code}
                    for c in unverified
                ],
                "event_ids": [c.event_id for c in unverified],
            },
        )
