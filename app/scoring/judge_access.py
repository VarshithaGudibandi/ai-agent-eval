from dataclasses import dataclass

from app.budget.reservations import BudgetOwner
from app.db import models
from app.db.budgets import budget_for_batch, budget_for_experiment
from app.db.judge_batches import in_running_batch
from app.db.scope import WorkspaceScope


@dataclass(frozen=True)
class JudgeAccess:
    """A run may be judged, and this budget pays for it."""

    owner: BudgetOwner
    budget_id: str


async def judge_access(
    scope: WorkspaceScope, run: models.Run, batch_id: str | None = None
) -> JudgeAccess | None:
    """Whether a run may be judged at all, and on whose budget.

    This is the one place build-loop and live runs differ. Everything after it, from the
    prompt to the stored verdict, is the same for both. A run that gets no access is
    graded by its deterministic scorers alone.
    """
    if run.experiment_id is not None:
        budget = await budget_for_experiment(scope, run.experiment_id)
        return JudgeAccess(BudgetOwner("experiment", run.experiment_id), budget.id)
    # A live run is judged only when someone asked for it: as part of a judge batch, started
    # by hand, by a schedule or by a new version. Its paid key was checked when the batch began.
    if batch_id is None or not await in_running_batch(scope, batch_id, run.id):
        return None
    budget = await budget_for_batch(scope, batch_id)
    return JudgeAccess(BudgetOwner("judge_batch", batch_id), budget.id)
