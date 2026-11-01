from app.environments.contract import TaskVersion
from app.evaluators.contract import EvalContext, Layer, Score
from app.evaluators.retail_spec import RetailSpec, is_retail
from app.evaluators.scores import verdict


class TicketOutcome:
    """Checks the support ticket ended in a status the task allows."""

    name = "ticket_outcome"
    version = "1.0.0"
    layer: Layer = "outcome"

    def applies_to(self, task: TaskVersion) -> bool:
        # A plain key check: a malformed spec must surface in evaluate, as an evaluation error.
        return is_retail(task) and task.evaluator_spec.get("expected_ticket") is not None

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        expected = RetailSpec.model_validate(ctx.task.evaluator_spec).expected_ticket
        if expected is None:
            return []
        ticket = next(
            (
                t
                for t in ctx.final_snapshot.get("tickets", [])
                if t["ticket_id"] == expected.ticket_id
            ),
            None,
        )
        observed = ticket["status"] if ticket else None
        if observed in expected.status_in:
            return [verdict(self, "ticket_status", "pass")]
        updates = [
            c.event_id
            for c in ctx.tool_calls()
            if c.tool == "update_ticket" and c.arguments.get("ticket_id") == expected.ticket_id
        ]
        event_ids = updates or [i for i in [ctx.finish_event_id()] if i]
        return [
            verdict(
                self,
                "ticket_status",
                "fail",
                reason="WRONG_TICKET_STATUS",
                evidence={
                    "ticket_id": expected.ticket_id,
                    "expected_status_in": expected.status_in,
                    "observed_status": observed,
                    "event_ids": event_ids,
                },
            )
        ]
