import json
from collections import defaultdict

from app.evaluators.contract import EvalContext, Score, ToolCallView
from app.evaluators.scores import Named, not_applicable, verdict
from app.evaluators.spec_parts import CallPattern, OrderRule

# Checks on the path an agent took that any environment can use with its own spec.


def required_calls(
    evaluator: Named, ctx: EvalContext, required: list[CallPattern], calls: list[ToolCallView]
) -> Score:
    if not required:
        return not_applicable(evaluator, "required_calls")
    # One observed call satisfies at most one requirement, so repeating a correct call
    # cannot inflate recall.
    available = [c for c in calls if c.status == "ok"]
    matched: list[ToolCallView] = []
    missing = []
    for pattern in required:
        hit = next((c for c in available if pattern.matches(c)), None)
        if hit is None:
            missing.append(pattern.label())
        else:
            available.remove(hit)
            matched.append(hit)
    recall = len(matched) / len(required)
    evidence = {
        "recall": recall,
        "matched": len(matched),
        "required": len(required),
        "attempted_calls": len(calls),
        "valid_calls": sum(1 for c in calls if c.error_code != "INVALID_ARGUMENTS"),
    }
    if not missing:
        return verdict(evaluator, "required_calls", "pass", evidence=evidence)
    return verdict(
        evaluator,
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


def call_order(evaluator: Named, rules: list[OrderRule], calls: list[ToolCallView]) -> Score:
    checked = False
    problems = []
    for index, call in enumerate(calls):
        for rule in rules:
            if call.tool != rule.tool or call.status == "rejected":
                continue
            checked = True
            key = call.arguments.get(rule.match_arg) if rule.match_arg else None
            earlier = [
                c
                for c in calls[:index]
                if c.status == "ok"
                and (key is None or c.arguments.get(rule.match_arg or "") == key)
            ]
            done = {c.tool for c in earlier}
            missing = [tool for tool in rule.after if tool not in done]
            if missing:
                problems.append((call, missing))
    if not checked:
        return not_applicable(evaluator, "call_order")
    if not problems:
        return verdict(evaluator, "call_order", "pass")
    return verdict(
        evaluator,
        "call_order",
        "fail",
        reason="MISSING_PREREQUISITE",
        evidence={
            "calls": [
                {"tool": c.tool, "arguments": c.arguments, "missing": m} for c, m in problems
            ],
            "event_ids": [c.request_event_id for c, _ in problems],
        },
    )


def repetition(evaluator: Named, max_identical: int, calls: list[ToolCallView]) -> Score:
    if not calls:
        return not_applicable(evaluator, "repetition")
    groups: dict[str, list[ToolCallView]] = defaultdict(list)
    for call in calls:
        groups[json.dumps([call.tool, call.arguments], sort_keys=True)].append(call)
    repeated = [g for g in groups.values() if len(g) > max_identical]
    if not repeated:
        return verdict(evaluator, "repetition", "pass")
    return verdict(
        evaluator,
        "repetition",
        "fail",
        reason="EXCESSIVE_REPETITION",
        evidence={
            "max_identical_calls": max_identical,
            "repeated": [
                {"tool": g[0].tool, "arguments": g[0].arguments, "count": len(g)} for g in repeated
            ],
            "event_ids": [c.request_event_id for g in repeated for c in g],
        },
    )
