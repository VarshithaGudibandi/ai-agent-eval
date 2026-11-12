from dataclasses import dataclass

from app.db import models
from app.db.budgets import budget_for_experiment
from app.db.scope import WorkspaceScope


@dataclass(frozen=True)
class JudgeAccess:
    """A run may be judged, and this experiment's budget pays for it."""

    experiment_id: str
    budget_id: str


async def judge_access(scope: WorkspaceScope, run: models.Run) -> JudgeAccess | None:
    """Whether a run may be judged at all, and on whose budget.

    This is the one place build-loop and live runs differ. Everything after it, from the
    prompt to the stored verdict, is the same for both. A run that gets no access is
    graded by its deterministic scorers alone.
    """
    if run.experiment_id is None:
        # Live runs are not judged yet. Judging them needs a sampling rule, a budget for
        # the whole workspace, and a paid key, since free-tier traffic may be used by the
        # provider.
        return None
    budget = await budget_for_experiment(scope, run.experiment_id)
    return JudgeAccess(run.experiment_id, budget.id)
