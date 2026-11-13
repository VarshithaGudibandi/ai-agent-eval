from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import distinct_on, insert

from app.db import models
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.explain.groups import RunFailures
from app.metrics.health import RunOutcome


@dataclass(frozen=True)
class Loaded:
    outcomes: list[RunOutcome]
    # Eval spec versions the newest verdicts came from.
    spec_ids: frozenset[str]


async def _hard_rule_keys(scope: WorkspaceScope, spec_ids: set[str]) -> dict[str, set[str]]:
    rows = await scope.session.execute(
        select(models.EvalSpecScorer.eval_spec_id, models.Scorer.key)
        .join(models.Scorer, models.Scorer.id == models.EvalSpecScorer.scorer_id)
        .where(
            models.EvalSpecScorer.workspace_id == scope.workspace_id,
            models.EvalSpecScorer.eval_spec_id.in_(spec_ids),
            models.Scorer.severity == models.Severity.HARD,
            models.Scorer.kind != models.ScorerKind.JUDGE,
        )
    )
    keys: dict[str, set[str]] = {}
    for spec_id, key in rows:
        keys.setdefault(spec_id, set()).add(key)
    return keys


async def _load(scope: WorkspaceScope, runs: Select[models.Run]) -> Loaded:
    run_rows = await scope.all(runs)
    run_ids = [r.id for r in run_rows]
    newest = (
        await scope.session.execute(
            select(models.Verdict)
            .where(
                models.Verdict.workspace_id == scope.workspace_id,
                models.Verdict.run_id.in_(run_ids),
            )
            .ext(distinct_on(models.Verdict.run_id))
            .order_by(models.Verdict.run_id, models.Verdict.created_at.desc())
        )
    ).scalars()
    verdicts = {v.run_id: v for v in newest}
    case_rows = await scope.session.execute(
        select(models.DatasetCase.id, models.DatasetCase.case_key).where(
            models.DatasetCase.workspace_id == scope.workspace_id,
            models.DatasetCase.id.in_([r.case_id for r in run_rows if r.case_id]),
        )
    )
    case_keys = {case_id: key for case_id, key in case_rows}
    pending = set(
        await scope.session.scalars(
            select(models.JudgeAnswer.run_id).where(
                models.JudgeAnswer.workspace_id == scope.workspace_id,
                models.JudgeAnswer.run_id.in_(run_ids),
                models.JudgeAnswer.trusted.is_(False),
                models.JudgeAnswer.answer != models.JudgeAnswerValue.CANT_TELL,
            )
        )
    )
    spec_ids = {v.eval_spec_id for v in verdicts.values()}
    hard = await _hard_rule_keys(scope, spec_ids)
    outcomes = []
    for run in run_rows:
        verdict = verdicts.get(run.id)
        failures = (
            tuple(k for k in verdict.failed if k in hard.get(verdict.eval_spec_id, set()))
            if verdict
            else ()
        )
        outcomes.append(
            RunOutcome(
                run_id=run.id,
                case_key=case_keys.get(run.case_id) if run.case_id else None,
                status=verdict.status if verdict else None,
                duration_ms=run.duration_ms,
                cost_micros=run.cost_micros,
                cost_known=run.cost_known,
                hard_failures=failures,
                pending_calibration=run.id in pending,
            )
        )
    return Loaded(outcomes, frozenset(spec_ids))


async def experiment_outcomes(scope: WorkspaceScope, experiment_id: str) -> Loaded:
    return await _load(
        scope, scope.query(models.Run).where(models.Run.experiment_id == experiment_id)
    )


async def live_outcomes(
    scope: WorkspaceScope,
    agent_id: str,
    version_id: str | None,
    start: datetime,
    end: datetime,
) -> Loaded:
    """Live runs of an agent, or of one version of it, that started in [start, end)."""
    statement = (
        scope.query(models.Run)
        .join(models.AgentVersion, models.AgentVersion.id == models.Run.agent_version_id)
        .where(
            models.AgentVersion.agent_id == agent_id,
            models.Run.experiment_id.is_(None),
            models.Run.started_at >= start,
            models.Run.started_at < end,
        )
    )
    if version_id is not None:
        statement = statement.where(models.Run.agent_version_id == version_id)
    return await _load(scope, statement)


async def baseline_experiment(
    scope: WorkspaceScope, candidate: models.Experiment
) -> models.Experiment | None:
    """What a new experiment is compared with: the newest finished experiment on the same
    dataset version of the version last marked shipped, or else of any other version."""
    version = await scope.require(models.AgentVersion, candidate.agent_version_id)
    agent = await scope.require(models.Agent, version.agent_id)
    others = (
        scope.query(models.Experiment)
        .join(models.AgentVersion, models.AgentVersion.id == models.Experiment.agent_version_id)
        .where(
            models.AgentVersion.agent_id == agent.id,
            models.Experiment.dataset_version_id == candidate.dataset_version_id,
            models.Experiment.status == models.ExperimentStatus.FINISHED,
            models.Experiment.agent_version_id != candidate.agent_version_id,
        )
        .order_by(models.Experiment.created_at.desc())
    )
    if agent.shipped_version_id and agent.shipped_version_id != candidate.agent_version_id:
        shipped = await scope.first(
            others.where(models.Experiment.agent_version_id == agent.shipped_version_id)
        )
        if shipped is not None:
            return shipped
    return await scope.first(others)


async def save_aggregate(
    scope: WorkspaceScope, version_id: str, day: date, runs: int, health: dict[str, Any]
) -> None:
    """Store a day's health, replacing what was stored for that version and day."""
    await scope.session.execute(
        insert(models.Aggregate)
        .values(
            id=new_id("agg"),
            workspace_id=scope.workspace_id,
            agent_version_id=version_id,
            day=day,
            runs=runs,
            health=health,
        )
        .on_conflict_do_update(
            index_elements=["agent_version_id", "day"],
            set_={"runs": runs, "health": health, "updated_at": func.now()},
        )
    )


async def daily_aggregates(
    scope: WorkspaceScope, agent_id: str, since: date
) -> list[models.Aggregate]:
    return await scope.all(
        scope.query(models.Aggregate)
        .join(models.AgentVersion, models.AgentVersion.id == models.Aggregate.agent_version_id)
        .where(models.AgentVersion.agent_id == agent_id, models.Aggregate.day >= since)
        .order_by(models.Aggregate.day, models.Aggregate.agent_version_id)
    )


async def batch_outcomes(scope: WorkspaceScope, batch_id: str) -> Loaded:
    members = select(models.JudgeBatchRun.run_id).where(
        models.JudgeBatchRun.workspace_id == scope.workspace_id,
        models.JudgeBatchRun.judge_batch_id == batch_id,
    )
    return await _load(scope, scope.query(models.Run).where(models.Run.id.in_(members)))


async def run_failures(scope: WorkspaceScope, run_ids: list[str]) -> list[RunFailures]:
    """What each failing run failed, by its newest verdict: scorer keys, their reasons, and
    the trusted judge questions answered no."""
    loaded = await _load(scope, scope.query(models.Run).where(models.Run.id.in_(run_ids)))
    failing = {o.run_id: o for o in loaded.outcomes if o.status == "failed"}
    if not failing:
        return []
    newest = (
        await scope.session.execute(
            select(models.Verdict)
            .where(
                models.Verdict.workspace_id == scope.workspace_id,
                models.Verdict.run_id.in_(list(failing)),
            )
            .ext(distinct_on(models.Verdict.run_id))
            .order_by(models.Verdict.run_id, models.Verdict.created_at.desc())
        )
    ).scalars()
    failed = {v.run_id: tuple(v.failed) for v in newest}
    scores = await scope.session.execute(
        select(models.Score.run_id, models.Scorer.key, models.Score.reason)
        .join(models.Scorer, models.Scorer.id == models.Score.scorer_id)
        .where(
            models.Score.workspace_id == scope.workspace_id,
            models.Score.run_id.in_(list(failing)),
            models.Score.passed.is_(False),
        )
    )
    reasons: dict[str, dict[str, str]] = {}
    for run_id, key, reason in scores:
        reasons.setdefault(run_id, {}).setdefault(key, reason)
    answers = await scope.session.execute(
        select(models.JudgeAnswer.run_id, models.Scorer.key, models.JudgeAnswer.question_id)
        .join(models.Scorer, models.Scorer.id == models.JudgeAnswer.scorer_id)
        .where(
            models.JudgeAnswer.workspace_id == scope.workspace_id,
            models.JudgeAnswer.run_id.in_(list(failing)),
            models.JudgeAnswer.answer == models.JudgeAnswerValue.NO,
            models.JudgeAnswer.trusted.is_(True),
        )
    )
    questions: dict[str, dict[str, list[str]]] = {}
    for run_id, key, question in answers:
        questions.setdefault(run_id, {}).setdefault(key, []).append(question)
    return [
        RunFailures(
            run_id=run_id,
            case_key=outcome.case_key,
            failed=failed.get(run_id, ()),
            reasons=reasons.get(run_id, {}),
            failed_questions={k: tuple(v) for k, v in questions.get(run_id, {}).items()},
        )
        for run_id, outcome in failing.items()
    ]


async def main_version(scope: WorkspaceScope, run_ids: list[str]) -> models.AgentVersion | None:
    """The agent version most of these runs ran on."""
    rows = await scope.session.execute(
        select(models.Run.agent_version_id, func.count())
        .where(models.Run.workspace_id == scope.workspace_id, models.Run.id.in_(run_ids))
        .group_by(models.Run.agent_version_id)
        .order_by(func.count().desc(), models.Run.agent_version_id)
        .limit(1)
    )
    found = rows.first()
    return None if found is None else await scope.require(models.AgentVersion, found[0])
