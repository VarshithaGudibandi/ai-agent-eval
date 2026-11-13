from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from app.api import agent_versions, agents, runs
from app.api.dependencies import Db
from app.db import models
from app.web.formatting import version_name
from app.web.rendering import Person, render
from app.web.trace_view import selected_span, span_rows, tokens_per_second

router = APIRouter(include_in_schema=False)


@router.get("/runs/{run_id}")
async def run_page(
    request: Request,
    run_id: str,
    principal: Person,
    session: Db,
    span: Annotated[str | None, Query(max_length=200)] = None,
) -> Response:
    run = await runs.run(run_id, principal, session)
    version = await agent_versions.version(run.agent_version_id, principal, session)
    agent = await agents.agent(version.agent_id, principal, session)
    rows = span_rows(run.spans)
    selected = selected_span(rows, run.scores, span)
    kinds = [s.kind for s in run.spans]
    return render(
        request,
        "run.html",
        principal=principal,
        can_edit=principal.role in (models.Role.MEMBER, models.Role.OWNER),
        run=run,
        agent=agent,
        version_name=version_name(version),
        verdict=run.verdicts[-1] if run.verdicts else None,
        root=rows[0].span,
        rows=rows,
        selected=selected,
        speed=tokens_per_second(selected.span),
        model_steps=kinds.count("model"),
        tool_steps=kinds.count("tool"),
        others_passed=sum(
            r.status == models.VerdictStatus.PASSED for r in run.repetitions if r.run_id != run.id
        ),
    )
