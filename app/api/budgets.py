from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.dependencies import Db, Viewer, scope_for
from app.db import models
from app.db.budgets import recent_budgets

router = APIRouter(prefix="/api/v1/budgets", tags=["budgets"])


class BudgetOut(BaseModel):
    id: str
    owner_kind: Literal["experiment", "judge_batch", "regression_report"]
    owner_id: str
    currency: str
    # None means no cap.
    money_limit_micros: int | None
    tokens_limit: int | None
    requests_limit: int | None
    # Includes amounts reserved for calls still in flight.
    money_used_micros: int
    tokens_used: int
    requests_used: int
    updated_at: datetime


def _out(row: models.Budget) -> BudgetOut:
    if row.experiment_id is not None:
        kind, owner = "experiment", row.experiment_id
    elif row.judge_batch_id is not None:
        kind, owner = "judge_batch", row.judge_batch_id
    else:
        kind, owner = "regression_report", str(row.regression_report_id)
    return BudgetOut(
        id=row.id,
        owner_kind=kind,
        owner_id=owner,
        currency=row.currency,
        money_limit_micros=row.money_limit_micros,
        tokens_limit=row.tokens_limit,
        requests_limit=row.requests_limit,
        money_used_micros=row.money_used_micros,
        tokens_used=row.tokens_used,
        requests_used=row.requests_used,
        updated_at=row.updated_at,
    )


@router.get("")
async def budgets(
    principal: Viewer, session: Db, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> list[BudgetOut]:
    """Recent budgets and what each has used: one per experiment, judge batch and report."""
    return [_out(row) for row in await recent_budgets(scope_for(session, principal), limit)]
