from typing import TYPE_CHECKING

from app.db.eval_specs import spec_at
from app.db.jobs import enqueue, require_lease
from app.db.scope import WorkspaceScope
from app.db.scores import NewScore, save_scores, save_verdict, scoring_input
from app.metrics.aggregate_job import queue_aggregate
from app.scoring.engine import score, verdict
from app.scoring.judge_job import queue_judging
from app.scoring.run_view import RunView
from app.scoring.specs import scorers_for

if TYPE_CHECKING:
    from app.jobs.worker import JobContext

SCORE_RUN = "score_run"


async def queue_scoring(scope: WorkspaceScope, run_id: str, spec_id: str | None = None) -> bool:
    """Queue a run to be scored. Without spec_id, the spec in force when its trace arrived
    is used. False if the run is already waiting to be scored."""
    payload = {"eval_spec_id": spec_id} if spec_id else {}
    return await enqueue(scope, SCORE_RUN, run_id, payload) is not None


async def score_run_job(ctx: "JobContext") -> None:
    """Score one run with its deterministic scorers, in one transaction fenced on the lease.

    The verdict is stored here too, unless judges apply: then it waits for the judge job.
    """
    claim = ctx.claim
    async with ctx.sessionmaker.begin() as session:
        await require_lease(session, claim.workspace_id, claim.job_id, claim.lease_token)
        scope = WorkspaceScope(session, claim.workspace_id)
        data = await scoring_input(scope, claim.subject_id)
        spec_id = claim.payload.get("eval_spec_id")
        if spec_id is None:
            spec = await spec_at(scope, data.agent_id, data.received_at)
            if spec is None:
                return
            spec_id = spec.id
        scorers = await scorers_for(scope, spec_id)
        run = RunView.build(
            data.spans, data.run.tags, data.config, data.expected, data.has_expected
        )
        results = score(run, scorers)
        await save_scores(
            scope,
            data.run.id,
            [
                NewScore(
                    r.scorer_id, r.span_id, r.value, r.passed, r.reason, list(r.evidence), r.error
                )
                for r in results
            ],
        )
        # With judges to come, the verdict waits for them; the judge job stores it.
        if await queue_judging(scope, data.run, run, scorers, spec_id):
            return
        result = verdict(scorers, results)
        await save_verdict(
            scope,
            data.run.id,
            spec_id,
            result.status,
            result.quality,
            list(result.failed),
            list(result.errored),
        )
        await queue_aggregate(scope, data.run)
