from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on, insert

from app.db import models
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.db.traces import spans_of


@dataclass(frozen=True)
class ScoringInput:
    run: models.Run
    agent_id: str
    config: dict[str, Any]
    received_at: datetime
    spans: list[models.Span]
    expected: Any
    has_expected: bool


async def scoring_input(scope: WorkspaceScope, run_id: str) -> ScoringInput:
    run = await scope.require(models.Run, run_id)
    version = await scope.require(models.AgentVersion, run.agent_version_id)
    trace = await scope.require(models.Trace, run.trace_row_id)
    case = await scope.get(models.DatasetCase, run.case_id) if run.case_id else None
    return ScoringInput(
        run=run,
        agent_id=version.agent_id,
        config=version.config,
        received_at=trace.received_at,
        spans=await spans_of(scope, run_id),
        expected=case.expected if case else None,
        has_expected=case is not None and case.expected is not None,
    )


@dataclass(frozen=True)
class NewScore:
    scorer_id: str
    span_id: str | None
    value: float | None
    passed: bool | None
    reason: str
    evidence: list[str]
    error: str | None
    confidence: float | None = None


async def save_scores(scope: WorkspaceScope, run_id: str, scores: list[NewScore]) -> None:
    """Store a run's scores. A score already stored for the same target is kept as it is."""
    if not scores:
        return
    await scope.session.execute(
        insert(models.Score)
        .values(
            [
                {
                    "id": new_id("sc"),
                    "workspace_id": scope.workspace_id,
                    "run_id": run_id,
                    **vars(score),
                }
                for score in scores
            ]
        )
        .on_conflict_do_nothing(constraint="uq_scores_run_id_span_id_scorer_id")
    )


async def save_verdict(
    scope: WorkspaceScope,
    run_id: str,
    spec_id: str,
    status: str,
    quality: float | None,
    failed: list[str],
    errored: list[str],
) -> None:
    await scope.session.execute(
        insert(models.Verdict)
        .values(
            id=new_id("vd"),
            workspace_id=scope.workspace_id,
            run_id=run_id,
            eval_spec_id=spec_id,
            status=status,
            quality=quality,
            failed=failed,
            errored=errored,
        )
        # A live run's verdict from its rules is replaced when a judge batch adds judges.
        .on_conflict_do_update(
            index_elements=["run_id", "eval_spec_id"],
            set_={"status": status, "quality": quality, "failed": failed, "errored": errored},
        )
    )


@dataclass(frozen=True)
class StoredScore:
    score: models.Score
    scorer: models.Scorer


async def scores_of(scope: WorkspaceScope, run_id: str) -> list[StoredScore]:
    rows = await scope.session.execute(
        select(models.Score, models.Scorer)
        .join(models.Scorer, models.Scorer.id == models.Score.scorer_id)
        .where(models.Score.workspace_id == scope.workspace_id, models.Score.run_id == run_id)
        .order_by(models.Scorer.key, models.Score.span_id)
    )
    return [StoredScore(score, scorer) for score, scorer in rows]


async def verdicts_of(
    scope: WorkspaceScope, run_id: str
) -> list[tuple[models.Verdict, models.EvalSpec]]:
    rows = await scope.session.execute(
        select(models.Verdict, models.EvalSpec)
        .join(models.EvalSpec, models.EvalSpec.id == models.Verdict.eval_spec_id)
        .where(models.Verdict.workspace_id == scope.workspace_id, models.Verdict.run_id == run_id)
        .order_by(models.EvalSpec.version)
    )
    return [(verdict, spec) for verdict, spec in rows]


@dataclass(frozen=True)
class ExperimentScoring:
    runs_scored: int
    runs_passed: int
    # How many runs each scorer failed, by scorer key.
    failures: dict[str, int]
    # How many runs each scorer could not grade, such as a judge with no key to call.
    errors: dict[str, int]


async def experiment_scoring(scope: WorkspaceScope, experiment_id: str) -> ExperimentScoring:
    """Counts over each run's newest verdict, so a rescored run counts once."""
    newest = (
        select(models.Verdict.status, models.Verdict.failed, models.Verdict.errored)
        .join(models.Run, models.Run.id == models.Verdict.run_id)
        .where(
            models.Verdict.workspace_id == scope.workspace_id,
            models.Run.experiment_id == experiment_id,
        )
        .ext(distinct_on(models.Verdict.run_id))
        .order_by(models.Verdict.run_id, models.Verdict.created_at.desc())
    )
    rows = (await scope.session.execute(newest)).all()
    failures: dict[str, int] = {}
    errors: dict[str, int] = {}
    for _, failed, errored in rows:
        for key in failed:
            failures[key] = failures.get(key, 0) + 1
        for key in errored:
            errors[key] = errors.get(key, 0) + 1
    passed = sum(1 for status, _, _ in rows if status == models.VerdictStatus.PASSED)
    return ExperimentScoring(
        len(rows), passed, dict(sorted(failures.items())), dict(sorted(errors.items()))
    )


async def run_ids(scope: WorkspaceScope, agent_id: str, experiment_id: str | None) -> list[str]:
    statement = (
        select(models.Run.id)
        .join(models.AgentVersion, models.AgentVersion.id == models.Run.agent_version_id)
        .where(
            models.Run.workspace_id == scope.workspace_id, models.AgentVersion.agent_id == agent_id
        )
        .order_by(models.Run.created_at)
    )
    if experiment_id is not None:
        statement = statement.where(models.Run.experiment_id == experiment_id)
    return list(await scope.session.scalars(statement))


async def scores_by(
    scope: WorkspaceScope, run_id: str, scorer_ids: list[str]
) -> list[models.Score]:
    return await scope.all(
        scope.query(models.Score).where(
            models.Score.run_id == run_id, models.Score.scorer_id.in_(scorer_ids)
        )
    )
