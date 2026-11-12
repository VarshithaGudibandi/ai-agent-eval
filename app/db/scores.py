from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert

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
        .on_conflict_do_nothing(index_elements=["run_id", "eval_spec_id"])
    )
