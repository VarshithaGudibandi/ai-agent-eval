from app.evaluators.contract import Evaluator
from app.evaluators.outcome.refund_outcome import RefundOutcome
from app.evaluators.outcome.ticket_outcome import TicketOutcome
from app.evaluators.policy.retail_policy import RetailPolicy
from app.evaluators.response.retail_response import RetailResponse
from app.evaluators.trajectory.retail_trajectory import RetailTrajectory


def default_evaluators() -> list[Evaluator]:
    return [RefundOutcome(), TicketOutcome(), RetailPolicy(), RetailTrajectory(), RetailResponse()]
