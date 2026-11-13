from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import BaseModel

from app.api.dependencies import Db, Member, Viewer, scope_for
from app.db import models
from app.db.comparisons import comparisons_of
from app.metrics.comparison import ComparisonResult
from app.metrics.comparisons import NewComparison, run_comparison

router = APIRouter(prefix="/api/v1/comparisons", tags=["comparisons"])


class ComparisonOut(BaseModel):
    id: str
    baseline: dict[str, Any]
    candidate: dict[str, Any]
    gates: dict[str, Any]
    created_at: datetime
    result: ComparisonResult


def _out(row: models.Comparison) -> ComparisonOut:
    return ComparisonOut(
        id=row.id,
        baseline=row.baseline,
        candidate=row.candidate,
        gates=row.gates,
        created_at=row.created_at,
        result=ComparisonResult.model_validate(row.result),
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(body: NewComparison, principal: Member, session: Db) -> ComparisonOut:
    """Compare two experiments, paired case by case, or two windows of live runs.

    Returns each metric's change with a 95% interval, the cases that got worse first, and
    ship, hold or insufficient_data from the gates.
    """
    scope = scope_for(session, principal)
    row, _ = await run_comparison(scope, body)
    await session.refresh(row)
    return _out(row)


@router.get("")
async def comparisons(
    principal: Viewer,
    session: Db,
    candidate_experiment_id: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[ComparisonOut]:
    """Comparisons newest first: all of them, or those whose candidate is one experiment, such
    as the one evalctl run stored for it."""
    found = await comparisons_of(scope_for(session, principal), candidate_experiment_id, limit)
    return [_out(row) for row in found]


@router.get("/{comparison_id}")
async def comparison(comparison_id: str, principal: Viewer, session: Db) -> ComparisonOut:
    """A stored comparison."""
    return _out(await scope_for(session, principal).require(models.Comparison, comparison_id))
