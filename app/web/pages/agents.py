from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.api import agent_versions, agents, datasets, experiments, reports
from app.api.dependencies import Db
from app.db import models
from app.explain.diff import tool_names
from app.web.formatting import version_name
from app.web.health_view import tiles
from app.web.rendering import Person, render

router = APIRouter(include_in_schema=False)

# The timeline shows the newest versions only; older ones are in the API.
TIMELINE = 10


@router.get("/agents")
async def agent_list(request: Request, principal: Person, session: Db) -> Response:
    return render(
        request, "agents.html", principal=principal, agents=await agents.agents(principal, session)
    )


@router.get("/agents/{agent_id}")
async def overview(request: Request, agent_id: str, principal: Person, session: Db) -> Response:
    agent = await agents.agent(agent_id, principal, session)
    health = await agents.health(agent_id, principal, session)
    experiment_id = health.current.experiment_id if health.current else None
    experiment = (
        await experiments.experiment(experiment_id, principal, session) if experiment_id else None
    )
    current_id = (
        health.current.agent_version_id
        if health.current and health.current.agent_version_id
        else agent.versions[-1].id
    )
    timeline = [
        await agent_versions.version(v.id, principal, session)
        for v in reversed(agent.versions[-TIMELINE:])
    ]
    report = None
    if health.mode == "versions" and experiment_id:
        found = await reports.reports(
            principal, session, candidate_experiment_id=experiment_id, limit=5
        )
        report = next((r for r in found if r.status == models.ReportStatus.DONE and r.report), None)
    current = await agent_versions.version(current_id, principal, session)
    return render(
        request,
        "agent.html",
        principal=principal,
        agent=agent,
        names={v.id: version_name(v) for v in agent.versions},
        current=current,
        tools=tool_names(current.config),
        spec=(
            await agents.eval_spec(agent_id, principal, session)
            if agent.eval_spec_version
            else None
        ),
        health=health,
        tiles=tiles(health.current.health, health.metrics) if health.current else [],
        experiment=experiment,
        timeline=timeline,
        report=report,
        failures=await agents.failures(agent_id, principal, session, experiment_id=experiment_id),
        datasets=await datasets.datasets(principal, session),
        can_edit=principal.role in (models.Role.MEMBER, models.Role.OWNER),
    )
