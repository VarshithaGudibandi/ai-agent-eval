from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from app.api import (
    agent_versions,
    agents,
    comparisons,
    experiments,
    judge_batches,
    reports,
)
from app.api.agent_versions import VersionDetailOut
from app.api.dependencies import Db
from app.auth.principal import Principal
from app.db import models
from app.explain.diff import config_diff
from app.explain.report import ChangeOut
from app.web.formatting import version_name
from app.web.health_view import metric_rows
from app.web.rendering import Person, render

router = APIRouter(include_in_schema=False)


@dataclass(frozen=True)
class Side:
    """One side of a comparison, as the page names it."""

    title: str
    detail: str
    agent_id: str
    version: VersionDetailOut | None
    unfinished: bool


def _can_edit(principal: Principal) -> bool:
    return principal.role in (models.Role.MEMBER, models.Role.OWNER)


async def _side(descriptor: dict[str, Any], principal: Principal, session: Db) -> Side:
    version: VersionDetailOut | None
    if "experiment_id" in descriptor:
        experiment = await experiments.experiment(descriptor["experiment_id"], principal, session)
        found = await agent_versions.version(experiment.agent_version_id, principal, session)
        return Side(
            title=f"{version_name(found)} · experiment",
            detail=f"{experiment.runs_received} runs, each case {experiment.repetitions} times",
            agent_id=found.agent_id,
            version=found,
            unfinished=experiment.status != models.ExperimentStatus.FINISHED,
        )
    if "judge_batch_id" in descriptor:
        batch = await judge_batches.batch(descriptor["judge_batch_id"], principal, session)
        version = (
            await agent_versions.version(batch.agent_version_id, principal, session)
            if batch.agent_version_id
            else None
        )
        return Side(
            title=(version_name(version) if version else "all versions") + " · judge batch",
            detail=(
                f"{batch.runs} live runs, {batch.window_start:%b %d} to {batch.window_end:%b %d}"
            ),
            agent_id=batch.agent_id,
            version=version,
            unfinished=batch.status != models.JudgeBatchStatus.FINISHED,
        )
    version = (
        await agent_versions.version(descriptor["version_id"], principal, session)
        if descriptor.get("version_id")
        else None
    )
    start = datetime.fromisoformat(descriptor["start"])
    end = datetime.fromisoformat(descriptor["end"])
    return Side(
        title=(version_name(version) if version else "all versions") + " · live runs",
        detail=f"{start:%b %d} to {end:%b %d}",
        agent_id=descriptor["agent_id"],
        version=version,
        unfinished=False,
    )


@router.get("/compare")
async def compare_form(
    request: Request,
    principal: Person,
    session: Db,
    agent: Annotated[str | None, Query(max_length=100)] = None,
) -> Response:
    found = await agents.agents(principal, session)
    chosen = (
        await agents.agent(agent, principal, session) if agent else (found[0] if found else None)
    )
    runs = await agents.experiments(chosen.id, principal, session, limit=50) if chosen else []
    baseline = await experiments.baseline(runs[0].id, principal, session) if runs else None
    today = datetime.now(UTC).date()
    return render(
        request,
        "compare.html",
        principal=principal,
        can_edit=_can_edit(principal),
        agents=found,
        chosen=chosen,
        experiments=runs,
        names={v.id: version_name(v) for v in chosen.versions} if chosen else {},
        default_baseline=baseline.experiment_id if baseline else None,
        # Live windows default to the last week against the week before.
        windows={
            "baseline_start": today - timedelta(days=14),
            "baseline_end": today - timedelta(days=7),
            "candidate_start": today - timedelta(days=7),
            "candidate_end": today + timedelta(days=1),
        },
    )


@router.get("/comparisons/{comparison_id}")
async def comparison_page(
    request: Request, comparison_id: str, principal: Person, session: Db
) -> Response:
    found = await comparisons.comparison(comparison_id, principal, session)
    base = await _side(found.baseline, principal, session)
    cand = await _side(found.candidate, principal, session)
    changes: list[ChangeOut] = []
    if base.version and cand.version and base.version.id != cand.version.id:
        changes = [
            ChangeOut(field=c.field, summary=c.summary, detail=list(c.detail))
            for c in config_diff(base.version.config, cand.version.config)
        ]
    latest = await reports.reports(principal, session, comparison_id=comparison_id, limit=1)
    result = found.result
    return render(
        request,
        "comparison.html",
        principal=principal,
        can_edit=_can_edit(principal),
        comparison=found,
        result=result,
        agent=await agents.agent(cand.agent_id, principal, session),
        base=base,
        cand=cand,
        changes=changes,
        rows=metric_rows(result.baseline, result.candidate, result.metrics),
        counts={
            outcome: sum(c.outcome == outcome for c in result.cases)
            for outcome in ("worse", "better", "same")
        },
        report=latest[0] if latest else None,
        anything_worse=result.decision == "hold"
        or any(m.worse and m.delta for m in result.metrics)
        or any(c.outcome == "worse" for c in result.cases),
    )
