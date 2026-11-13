from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from app.api.dependencies import Db, Member, Viewer, get_settings_dep, scope_for
from app.db import models
from app.db.audit import record_audit
from app.explain.job import request_report
from app.explain.report import Report
from app.settings import Settings

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


class NewReport(BaseModel):
    comparison_id: str
    # Leave out to use EXPLAINER_MODEL.
    model: str | None = Field(default=None, min_length=1, max_length=100)


class ReportOut(BaseModel):
    id: str
    comparison_id: str
    status: models.ReportStatus
    model: str
    created_at: datetime
    finished_at: datetime | None
    error: str | None
    report: Report | None


def _out(row: models.RegressionReport) -> ReportOut:
    return ReportOut(
        id=row.id,
        comparison_id=row.comparison_id,
        status=models.ReportStatus(row.status),
        model=row.model,
        created_at=row.created_at,
        finished_at=row.finished_at,
        error=row.error,
        report=None if row.report is None else Report.model_validate(row.report),
    )


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create(
    body: NewReport,
    principal: Member,
    session: Db,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> ReportOut:
    """Ask why a comparison's candidate got worse. The report is written by the worker.

    It names what changed in the config, groups the new failures, and for each group gives
    a likely cause that cites spans, with a confidence the evidence can carry. It uses the
    workspace's Gemini key, and at most ten model calls.
    """
    scope = scope_for(session, principal)
    row = await request_report(scope, body.comparison_id, body.model or settings.explainer_model)
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "report.requested",
        "regression_report",
        row.id,
        {"comparison_id": body.comparison_id},
    )
    await scope.flush()
    await session.refresh(row)
    return _out(row)


@router.get("/{report_id}")
async def report(report_id: str, principal: Viewer, session: Db) -> ReportOut:
    """A regression report, or its progress."""
    return _out(await scope_for(session, principal).require(models.RegressionReport, report_id))
