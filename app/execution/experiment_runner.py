from app.db import models
from app.db.experiments import complete_if_done, next_queued_run, start_attempt
from app.db.scope import WorkspaceScope
from app.execution.attempt_runner import Platform, run_attempt


async def run_experiment(
    platform: Platform, workspace_id: str, experiment_id: str
) -> models.ExperimentStatus:
    """Run queued runs one at a time until the experiment finishes or is paused.

    Before every attempt it re-reads the experiment, so a pause takes effect between runs
    and nothing that already finished is touched.
    """
    while True:
        async with platform.sessionmaker.begin() as session:
            scope = WorkspaceScope(session, workspace_id)
            experiment = await scope.require(models.Experiment, experiment_id)
            if experiment.status == models.ExperimentStatus.QUEUED:
                experiment.status = models.ExperimentStatus.RUNNING
            if experiment.status != models.ExperimentStatus.RUNNING:
                return models.ExperimentStatus(experiment.status)
            run = await next_queued_run(scope, experiment_id)
            if run is None:
                await complete_if_done(scope, experiment_id)
                return models.ExperimentStatus(experiment.status)
            attempt = await start_attempt(scope, run.id)
        await run_attempt(platform, workspace_id, attempt.id)
