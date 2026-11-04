from app.environments.contract import TaskVersion
from app.evaluators.contract import EvalContext, Layer, Score
from app.evaluators.it_spec import ItFacts, is_it
from app.evaluators.trajectory.checks import call_order, repetition, required_calls


class ItTrajectory:
    """Grades how the agent worked: required lookups, looking before acting, repetition."""

    name = "it_trajectory"
    version = "1.0.1"
    layer: Layer = "trajectory"

    def applies_to(self, task: TaskVersion) -> bool:
        return is_it(task)

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        spec = ItFacts(ctx).spec
        calls = ctx.tool_calls()
        return [
            required_calls(self, ctx, spec.required_calls, calls),
            call_order(self, spec.order_rules, calls),
            repetition(self, spec.max_identical_calls, calls),
        ]
