import uuid

from app.db import models
from app.db.experiments import DEFAULT_LEASE_SECONDS, complete_if_done
from app.db.jobs import claim_next
from app.db.scope import WorkspaceScope
from app.execution.attempt_runner import Platform, run_attempt


async def run_experiment(
    platform: Platform, workspace_id: str, experiment_id: str
) -> models.ExperimentStatus:
    """Work through one experiment's queue in this process, one job at a time.

    It claims jobs exactly as a worker does, so pausing, cancelling and the budget behave the
    same: a paused experiment hands out no more jobs, and nothing finished is touched.
    """
    worker_id = f"inline-{uuid.uuid4().hex[:8]}"
    while True:
        async with platform.sessionmaker.begin() as session:
            claim = await claim_next(
                session, worker_id, DEFAULT_LEASE_SECONDS, experiment_id=experiment_id
            )
            if claim is None:
                scope = WorkspaceScope(session, workspace_id)
                await complete_if_done(scope, experiment_id)
                experiment = await scope.require(models.Experiment, experiment_id)
                return models.ExperimentStatus(experiment.status)
        await run_attempt(platform, workspace_id, claim.attempt_id, claim.lease_token)
