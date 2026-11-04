from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from app.db import models
from app.db.scope import WorkspaceScope

# Everything a report needs about each run of an experiment, read in a handful of bulk
# queries. The numbers are computed from these facts in app/reports, which never queries.


@dataclass(frozen=True)
class ScoreFacts:
    evaluator: str
    version: str
    layer: str
    dimension: str
    applicable: bool
    status: str
    reason_code: str | None
    evidence: dict[str, Any]


@dataclass
class RunFacts:
    run_id: str
    agent_config_id: str
    task_version_id: str
    task_key: str
    category: str
    repetition: int
    execution_status: str
    evaluation_status: str
    # The latest attempt is the one that was graded.
    attempt_id: str | None = None
    attempts: int = 0
    queue_ms: int | None = None
    duration_ms: int | None = None
    scores: list[ScoreFacts] = field(default_factory=list)
    tool_calls_attempted: int = 0
    tool_calls_invalid: int = 0
    # Model usage counts every attempt, since retries cost money too.
    model_requests: int = 0
    model_tokens: int = 0
    cost_micros: int = 0
    cost_known: bool = True
    resolved_models: set[str] = field(default_factory=set)
    self_reported: dict[str, int] = field(default_factory=dict)


# A refusal for going over the tool-call limit is the platform stopping the agent, not a
# malformed call, so it does not count against schema validity.
_NOT_A_CALL = "TOOL_LIMIT_REACHED"
_INVALID = ("INVALID_ARGUMENTS", "TOOL_NOT_ALLOWED")


async def run_facts(scope: WorkspaceScope, experiment_id: str) -> list[RunFacts]:
    """One entry per planned run of the experiment, ordered by task, config and repetition."""
    experiment = await scope.require(models.Experiment, experiment_id)
    ws = scope.workspace_id
    rows = await scope.session.execute(
        select(models.Run, models.TaskVersion.task_key, models.TaskVersion.category)
        .join(
            models.TaskVersion,
            (models.TaskVersion.id == models.Run.task_version_id)
            & (models.TaskVersion.workspace_id == models.Run.workspace_id),
        )
        .where(models.Run.workspace_id == ws, models.Run.experiment_id == experiment.id)
        .order_by(models.TaskVersion.task_key, models.Run.agent_config_id, models.Run.repetition)
    )
    facts = {
        run.id: RunFacts(
            run_id=run.id,
            agent_config_id=run.agent_config_id,
            task_version_id=run.task_version_id,
            task_key=task_key,
            category=category,
            repetition=run.repetition,
            execution_status=run.execution_status,
            evaluation_status=run.evaluation_status,
        )
        for run, task_key, category in rows
    }
    if not facts:
        return []
    run_ids = list(facts)

    attempts = list(
        await scope.session.scalars(
            select(models.Attempt)
            .where(models.Attempt.workspace_id == ws, models.Attempt.run_id.in_(run_ids))
            .order_by(models.Attempt.run_id, models.Attempt.attempt_number)
        )
    )
    jobs = await scope.session.execute(
        select(models.Job.run_id, models.Job.created_at).where(
            models.Job.workspace_id == ws, models.Job.run_id.in_(run_ids)
        )
    )
    queued_at = {run_id: created_at for run_id, created_at in jobs}
    run_of_attempt: dict[str, str] = {}
    for attempt in attempts:
        run = facts[attempt.run_id]
        run_of_attempt[attempt.id] = run.run_id
        if run.attempts == 0 and attempt.started_at is not None and run.run_id in queued_at:
            waited = attempt.started_at - queued_at[run.run_id]
            run.queue_ms = max(0, round(waited.total_seconds() * 1000))
        run.attempts += 1
        run.attempt_id = attempt.id
        run.duration_ms = attempt.duration_ms
    graded = {r.attempt_id: r for r in facts.values() if r.attempt_id is not None}
    all_attempts = list(run_of_attempt)

    if graded:
        for score in await scope.session.scalars(
            select(models.Score)
            .where(models.Score.workspace_id == ws, models.Score.attempt_id.in_(list(graded)))
            .order_by(models.Score.evaluator_name, models.Score.dimension)
        ):
            graded[score.attempt_id].scores.append(
                ScoreFacts(
                    evaluator=score.evaluator_name,
                    version=score.evaluator_version,
                    layer=score.layer,
                    dimension=score.dimension,
                    applicable=score.applicable,
                    status=score.status,
                    reason_code=score.reason_code,
                    evidence=score.evidence,
                )
            )
        calls = await scope.session.execute(
            select(
                models.ToolCall.attempt_id,
                models.ToolCall.status,
                models.ToolCall.error_code,
                func.count(),
            )
            .where(models.ToolCall.workspace_id == ws, models.ToolCall.attempt_id.in_(list(graded)))
            .group_by(
                models.ToolCall.attempt_id, models.ToolCall.status, models.ToolCall.error_code
            )
        )
        for attempt_id, status, error_code, count in calls:
            if error_code == _NOT_A_CALL:
                continue
            run = graded[attempt_id]
            run.tool_calls_attempted += count
            if status == models.ToolCallStatus.REJECTED and error_code in _INVALID:
                run.tool_calls_invalid += count

    if all_attempts:
        usage = await scope.session.execute(
            select(
                models.ModelCall.attempt_id,
                models.ModelCall.resolved_model,
                models.ModelCall.cost_micros,
                func.coalesce(models.ModelCall.input_tokens, 0)
                + func.coalesce(models.ModelCall.cached_input_tokens, 0)
                + func.coalesce(models.ModelCall.output_tokens, 0)
                + func.coalesce(models.ModelCall.other_tokens, 0),
            ).where(
                models.ModelCall.workspace_id == ws,
                models.ModelCall.attempt_id.in_(all_attempts),
            )
        )
        for attempt_id, resolved, cost, tokens in usage:
            run = facts[run_of_attempt[attempt_id]]
            run.model_requests += 1
            run.model_tokens += int(tokens)
            if cost is None:
                run.cost_known = False
            else:
                run.cost_micros += cost
            if resolved:
                run.resolved_models.add(resolved)
        finished = await scope.session.execute(
            select(models.TraceEvent.attempt_id, models.TraceEvent.payload).where(
                models.TraceEvent.workspace_id == ws,
                models.TraceEvent.attempt_id.in_(all_attempts),
                models.TraceEvent.event_type == "agent_finished",
            )
        )
        for attempt_id, payload in finished:
            run = facts[run_of_attempt[attempt_id]]
            totals: defaultdict[str, int] = defaultdict(int, run.self_reported)
            for key, value in (payload.get("usage_self_reported") or {}).items():
                if isinstance(value, int):
                    totals[key] += value
            run.self_reported = dict(totals)
    return list(facts.values())
