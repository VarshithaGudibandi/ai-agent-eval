from app.evaluators.contract import Evaluator
from app.evaluators.outcome.refund_outcome import RefundOutcome


def default_evaluators() -> list[Evaluator]:
    return [RefundOutcome()]
