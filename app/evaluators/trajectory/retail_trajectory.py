import json
from collections import defaultdict

from app.environments.contract import TaskVersion
from app.evaluators.contract import EvalContext, Layer, Score, ToolCallView
from app.evaluators.retail_spec import UNKNOWN_OUTCOME, RetailFacts, RetailSpec, is_retail
from app.evaluators.scores import not_applicable, verdict


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
            self._required_calls(ctx, spec, calls),
            self._call_order(spec, calls),
            self._repetition(spec, calls),
            self._error_handling(calls),
        ]

    def _required_calls(
        self, ctx: EvalContext, spec: RetailSpec, calls: list[ToolCallView]
    ) -> Score:
        if not spec.required_calls:
            return not_applicable(self, "required_calls")
        # One observed call satisfies at most one requirement, so repeating a correct call
        # cannot inflate recall.
        available = [c for c in calls if c.status == "ok"]
        matched: list[ToolCallView] = []
        missing = []
        for required in spec.required_calls:
            hit = next((c for c in available if required.matches(c)), None)
            if hit is None:
                missing.append(required.label())
            else:
                available.remove(hit)
                matched.append(hit)
        recall = len(matched) / len(spec.required_calls)
        evidence = {
            "recall": recall,
            "matched": len(matched),
            "required": len(spec.required_calls),
            "attempted_calls": len(calls),
            "valid_calls": sum(1 for c in calls if c.error_code != "INVALID_ARGUMENTS"),
        }
        if not missing:
            return verdict(self, "required_calls", "pass", evidence=evidence)
        return verdict(
            self,
            "required_calls",
            "fail",
            reason="MISSING_REQUIRED_CALL",
            value=recall,
            evidence={
                **evidence,
                "missing": missing,
                "event_ids": [i for i in [ctx.finish_event_id()] if i],
            },
        )

    def _call_order(self, spec: RetailSpec, calls: list[ToolCallView]) -> Score:
        checked = False
        problems = []
        for index, call in enumerate(calls):
            for rule in spec.order_rules:
                if call.tool != rule.tool or call.status == "rejected":
                    continue
                checked = True
                order_id = call.arguments.get("order_id")
                earlier = [
                    c
                    for c in calls[:index]
                    if c.status == "ok"
                    and (
                        not rule.same_order
                        or order_id is None
                        or c.arguments.get("order_id") == order_id
                    )
                ]
                done = {c.tool for c in earlier}
                missing = [tool for tool in rule.after if tool not in done]
                if missing:
                    problems.append((call, missing))
        if not checked:
            return not_applicable(self, "call_order")
        if not problems:
            return verdict(self, "call_order", "pass")
        return verdict(
            self,
            "call_order",
            "fail",
            reason="MISSING_PREREQUISITE",
            evidence={
                "calls": [
                    {"tool": c.tool, "order_id": c.arguments.get("order_id"), "missing": m}
                    for c, m in problems
                ],
                "event_ids": [c.request_event_id for c, _ in problems],
            },
        )

    def _repetition(self, spec: RetailSpec, calls: list[ToolCallView]) -> Score:
        if not calls:
            return not_applicable(self, "repetition")
        groups: dict[str, list[ToolCallView]] = defaultdict(list)
        for call in calls:
            groups[json.dumps([call.tool, call.arguments], sort_keys=True)].append(call)
        repeated = [g for g in groups.values() if len(g) > spec.max_identical_calls]
        if not repeated:
            return verdict(self, "repetition", "pass")
        return verdict(
            self,
            "repetition",
            "fail",
            reason="EXCESSIVE_REPETITION",
            evidence={
                "max_identical_calls": spec.max_identical_calls,
                "repeated": [
                    {"tool": g[0].tool, "arguments": g[0].arguments, "count": len(g)}
                    for g in repeated
                ],
                "event_ids": [c.request_event_id for g in repeated for c in g],
            },
        )

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
