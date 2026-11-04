from datetime import datetime

from fastapi import APIRouter, status
from pydantic import BaseModel

from app.api.dependencies import Db, Member, Viewer, scope_for
from app.db import models
from app.db.audit import record_audit
from app.db.comparisons import list_comparisons, load_side, save_comparison
from app.reports.comparison import ComparisonResult, compare

router = APIRouter(prefix="/api/v1/comparisons", tags=["comparisons"])


class SideRequest(BaseModel):
    experiment_id: str
    # Needed only when the experiment ran more than one config.
    agent_config_id: str | None = None


class NewComparisonRequest(BaseModel):
    baseline: SideRequest
    candidate: SideRequest


class ComparisonOut(BaseModel):
    id: str
    created_at: datetime
    result: ComparisonResult

    @classmethod
    def of(cls, row: models.Comparison) -> "ComparisonOut":
        return cls(
            id=row.id,
            created_at=row.created_at,
            result=ComparisonResult.model_validate(row.result),
        )


class ComparisonSummaryOut(BaseModel):
    id: str
    created_at: datetime
    baseline_experiment_id: str
    baseline_config_id: str
    candidate_experiment_id: str
    candidate_config_id: str
    wins: int
    losses: int
    ties: int


@router.post("", status_code=status.HTTP_201_CREATED)
async def new_comparison(
    body: NewComparisonRequest, principal: Member, session: Db
) -> ComparisonOut:
    """Pair two configs task by task. Refused unless only the agent config differs.

    The result is frozen when it is made, so the report never shifts under its readers.
    """
    scope = scope_for(session, principal)
    baseline, baseline_runs = await load_side(
        scope, body.baseline.experiment_id, body.baseline.agent_config_id
    )
    candidate, candidate_runs = await load_side(
        scope, body.candidate.experiment_id, body.candidate.agent_config_id
    )
    result = compare(baseline, candidate, baseline_runs, candidate_runs)
    row = save_comparison(scope, baseline, candidate, result.model_dump(mode="json"))
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "comparison.created",
        "comparison",
        row.id,
        {"baseline": baseline.experiment_id, "candidate": candidate.experiment_id},
    )
    await scope.flush()
    await session.refresh(row)
    return ComparisonOut.of(row)


@router.get("")
async def comparisons(principal: Viewer, session: Db) -> list[ComparisonSummaryOut]:
    """Comparisons in this workspace, newest first."""
    return [
        ComparisonSummaryOut(
            id=row.id,
            created_at=row.created_at,
            baseline_experiment_id=row.baseline_experiment_id,
            baseline_config_id=row.baseline_config_id,
            candidate_experiment_id=row.candidate_experiment_id,
            candidate_config_id=row.candidate_config_id,
            wins=row.result["wins"],
            losses=row.result["losses"],
            ties=row.result["ties"],
        )
        for row in await list_comparisons(scope_for(session, principal))
    ]


@router.get("/{comparison_id}")
async def comparison(comparison_id: str, principal: Viewer, session: Db) -> ComparisonOut:
    """One comparison, regressed tasks first."""
    scope = scope_for(session, principal)
    return ComparisonOut.of(await scope.require(models.Comparison, comparison_id))
