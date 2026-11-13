from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.db import models
from app.db.budgets import BudgetLimits, create_budget
from app.db.eval_specs import require_current_spec
from app.db.ids import new_id
from app.db.jobs import enqueue
from app.db.judge_batches import add_batch_runs, live_run_ids
from app.db.scope import NotFoundError, WorkspaceScope
from app.providers.credentials import active_credential
from app.scoring.definitions import Tag
from app.scoring.judge_job import JUDGE_RUN
from app.scoring.judges import ASKS_WHEN_BORDERLINE
from app.scoring.specs import scorers_for

MAX_BATCH = 1000


class JudgingRefusedError(ValueError):
    """Live runs cannot be judged as asked, with the reason and what to do."""


class NewJudgeBatch(BaseModel):
    """Which stored live runs to judge: an agent's, or one version's, in a time window."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    version_id: str | None = None
    start: AwareDatetime
    end: AwareDatetime
    # Every tag must match, such as {"environment": "production"}.
    tags: dict[str, Tag] = Field(default_factory=dict, max_length=20)
    # At most this many runs, newest first. It bounds what the batch can spend.
    limit: int = Field(default=200, ge=1, le=MAX_BATCH)

    @model_validator(mode="after")
    def _ordered(self) -> "NewJudgeBatch":
        if self.end <= self.start:
            raise ValueError("a window must end after it starts")
        return self


async def start_batch(
    scope: WorkspaceScope, request: NewJudgeBatch, trigger: models.JudgeBatchTrigger
) -> models.JudgeBatch:
    """Pick the runs, give the batch a budget sized to them, and queue their judging."""
    await scope.require(models.Agent, request.agent_id)
    if request.version_id is not None:
        version = await scope.require(models.AgentVersion, request.version_id)
        if version.agent_id != request.agent_id:
            raise NotFoundError(f"agent_versions:{request.version_id}")
    credential = await active_credential(scope, "gemini")
    if credential is None or not credential.paid:
        raise JudgingRefusedError(
            "live judging needs a Gemini key marked paid: free-tier traffic may be used by"
            " the provider, and live traces are your customers' data"
        )
    spec = await require_current_spec(scope, request.agent_id)
    judges = [s for s in await scorers_for(scope, spec.id) if s.definition.is_judge]
    if not judges:
        raise JudgingRefusedError("the agent's eval spec has no judges to run")
    run_ids = await live_run_ids(
        scope,
        request.agent_id,
        request.version_id,
        request.start,
        request.end,
        request.tags,
        request.limit,
    )
    batch = models.JudgeBatch(
        id=new_id("jb"),
        workspace_id=scope.workspace_id,
        agent_id=request.agent_id,
        agent_version_id=request.version_id,
        eval_spec_id=spec.id,
        window_start=request.start,
        window_end=request.end,
        tags=request.tags,
        run_limit=request.limit,
        trigger=trigger,
        status=(models.JudgeBatchStatus.RUNNING if run_ids else models.JudgeBatchStatus.FINISHED),
    )
    scope.add(batch)
    await scope.flush()
    # Enough requests for every judge to ask three times on every run, and no more.
    requests = len(run_ids) * len(judges) * ASKS_WHEN_BORDERLINE
    await create_budget(scope, None, BudgetLimits(requests=requests), judge_batch_id=batch.id)
    await add_batch_runs(scope, batch.id, run_ids)
    for run_id in run_ids:
        await enqueue(
            scope, JUDGE_RUN, run_id, {"eval_spec_id": spec.id, "judge_batch_id": batch.id}
        )
    return batch
