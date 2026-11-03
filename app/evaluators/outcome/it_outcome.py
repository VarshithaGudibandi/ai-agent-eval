from typing import Any

from app.environments.contract import TaskVersion
from app.evaluators.contract import EvalContext, Layer, Score
from app.evaluators.it_spec import ItFacts, is_it
from app.evaluators.scores import not_applicable, verdict


class ItOutcome:
    """Compares the resets, grants and ticket queue the attempt left with what the task expects."""

    name = "it_outcome"
    version = "1.0.0"
    layer: Layer = "outcome"

    def applies_to(self, task: TaskVersion) -> bool:
        return is_it(task)

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        facts = ItFacts(ctx)
        resets = ctx.final_snapshot.get("password_resets", [])
        grants = ctx.final_snapshot.get("access_grants", [])
        return [
            self._compare(
                ctx,
                "password_resets",
                {r: r for r in facts.spec.expected_resets},
                {r["user_id"]: r for r in resets},
                "MISSING_RESET",
                "UNEXPECTED_RESET",
            ),
            self._compare(
                ctx,
                "access_grants",
                {(g.user_id, g.system): g for g in facts.spec.expected_grants},
                {(g["user_id"], g["system"]): g for g in grants},
                "MISSING_GRANT",
                "UNEXPECTED_GRANT",
            ),
            self._queue(ctx, facts),
        ]

    def _compare(
        self,
        ctx: EvalContext,
        dimension: str,
        expected: dict[Any, Any],
        observed: dict[Any, dict[str, Any]],
        missing_code: str,
        unexpected_code: str,
    ) -> Score:
        missing = sorted(set(expected) - set(observed), key=str)
        unexpected = sorted(set(observed) - set(expected), key=str)
        if not missing and not unexpected:
            return verdict(self, dimension, "pass")
        events = [
            e.id
            for key in unexpected
            if (e := ctx.tool_result_event(observed[key]["tool_call_id"])) is not None
        ]
        return verdict(
            self,
            dimension,
            "fail",
            # Something done that should not have been is worse than something left undone.
            reason=unexpected_code if unexpected else missing_code,
            evidence={
                "missing": [str(k) for k in missing],
                "unexpected": [str(k) for k in unexpected],
                "event_ids": events or [i for i in [ctx.finish_event_id()] if i],
            },
        )

    def _queue(self, ctx: EvalContext, facts: ItFacts) -> Score:
        expected = facts.spec.expected_queue
        if expected is None:
            return not_applicable(self, "ticket_queue")
        ticket = next(
            (
                t
                for t in ctx.final_snapshot.get("tickets", [])
                if t["ticket_id"] == expected.ticket_id
            ),
            None,
        )
        observed = ticket["queue"] if ticket else None
        if observed == expected.queue:
            return verdict(self, "ticket_queue", "pass")
        routes = [
            c.event_id
            for c in ctx.tool_calls()
            if c.tool == "route_ticket" and c.arguments.get("ticket_id") == expected.ticket_id
        ]
        return verdict(
            self,
            "ticket_queue",
            "fail",
            reason="WRONG_QUEUE",
            evidence={
                "ticket_id": expected.ticket_id,
                "expected_queue": expected.queue,
                "observed_queue": observed,
                "event_ids": routes or [i for i in [ctx.finish_event_id()] if i],
            },
        )
