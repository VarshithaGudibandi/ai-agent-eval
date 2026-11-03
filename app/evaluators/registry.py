from app.evaluators.contract import Evaluator
from app.evaluators.outcome.it_outcome import ItOutcome
from app.evaluators.outcome.refund_outcome import RefundOutcome
from app.evaluators.outcome.ticket_outcome import TicketOutcome
from app.evaluators.policy.it_policy import ItPolicy
from app.evaluators.policy.retail_policy import RetailPolicy
from app.evaluators.response.it_response import ItResponse
from app.evaluators.response.retail_response import RetailResponse
from app.evaluators.trajectory.it_trajectory import ItTrajectory
from app.evaluators.trajectory.retail_trajectory import RetailTrajectory


def default_evaluators() -> list[Evaluator]:
    return [
        RefundOutcome(),
        TicketOutcome(),
        RetailPolicy(),
        RetailTrajectory(),
        RetailResponse(),
        ItOutcome(),
        ItPolicy(),
        ItTrajectory(),
        ItResponse(),
    ]
