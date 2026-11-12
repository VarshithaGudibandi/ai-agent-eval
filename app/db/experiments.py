from app.db import models
from app.db.scope import WorkspaceScope


async def pause_for_budget(scope: WorkspaceScope, experiment_id: str) -> bool:
    """Pause a queued or running experiment. Returns True only if this call paused it."""
    experiment = await scope.session.scalar(
        scope.query(models.Experiment)
        .where(models.Experiment.id == experiment_id)
        .with_for_update()
    )
    if experiment is None or experiment.status not in (
        models.ExperimentStatus.QUEUED,
        models.ExperimentStatus.RUNNING,
    ):
        return False
    experiment.status = models.ExperimentStatus.PAUSED_BUDGET
    return True
