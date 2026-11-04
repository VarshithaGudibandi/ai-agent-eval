from collections.abc import Awaitable, Callable
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.api import (
    agent_configs,
    api_tokens,
    comparisons,
    experiments,
    provider_credentials,
    runs,
    signing_secret,
    workspaces,
)
from app.api.dependencies import SESSION_COOKIE, Db
from app.auth.principal import Principal, role_at_least
from app.auth.sessions import resolve_session
from app.db import models
from app.db.scope import NotFoundError
from app.web import formatting

# Server-rendered pages. Each page reads through the same handlers as the JSON API, so a page
# can never show what the API would not, and every change a page makes is a call to /api/v1
# from the browser.

TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name("templates"))
STATIC_DIR = Path(__file__).with_name("static")
formatting.register(TEMPLATES.env)

# Pages run no inline script and load nothing from elsewhere, so injected markup in an agent's
# reply or a tool result could not run even if escaping were ever missed.
SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}

router = APIRouter(include_in_schema=False)


class SignInRequiredError(Exception):
    pass


async def page_principal(request: Request, session: Db) -> Principal:
    cookie = request.cookies.get(SESSION_COOKIE)
    resolved = await resolve_session(session, cookie) if cookie else None
    if resolved is None:
        raise SignInRequiredError
    return resolved[0]


Person = Annotated[Principal, Depends(page_principal)]


def render(request: Request, template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, template, context, status_code=status_code, headers=SECURITY_HEADERS
    )


def page[**P](handler: Callable[P, Awaitable[Response]]) -> Callable[P, Awaitable[Response]]:
    """Show a plain not-found page for missing rows, which include other workspaces' rows."""

    @wraps(handler)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> Response:
        try:
            return await handler(*args, **kwargs)
        except NotFoundError:
            return render(cast(Request, kwargs["request"]), "not_found.html", status_code=404)

    return wrapped


@router.get("/login")
async def login(request: Request) -> HTMLResponse:
    return render(request, "login.html")


@router.get("/")
async def home() -> RedirectResponse:
    return RedirectResponse("/experiments", status_code=303)


async def _config_names(principal: Principal, session: Db) -> dict[str, str]:
    return {c.id: c.name for c in await agent_configs.configs(principal, session)}


@router.get("/experiments")
@page
async def experiment_list(request: Request, principal: Person, session: Db) -> Response:
    return render(
        request,
        "experiments.html",
        principal=principal,
        experiments=await experiments.experiments(principal, session),
        config_names=await _config_names(principal, session),
    )


async def _run_list(
    request: Request, principal: Principal, session: Db, experiment_id: str
) -> list[experiments.RunOut]:
    query = request.query_params
    return await experiments.runs(
        experiment_id,
        principal,
        session,
        evaluation_status=models.EvaluationStatus(query["status"]) if query.get("status") else None,
        category=query.get("category") or None,
        reason_code=query.get("reason_code") or None,
        limit=200,
        offset=0,
    )


@router.get("/experiments/{experiment_id}")
@page
async def experiment_detail(
    request: Request, experiment_id: str, principal: Person, session: Db
) -> Response:
    detail = await experiments.experiment(experiment_id, principal, session)
    return render(
        request,
        "experiment.html",
        principal=principal,
        experiment=detail,
        config_names=await _config_names(principal, session),
        runs=await _run_list(request, principal, session, experiment_id),
        filters=dict(request.query_params),
        can_change=role_at_least(principal.role, models.Role.MEMBER),
        is_owner=principal.role == models.Role.OWNER,
    )


@router.get("/experiments/{experiment_id}/runs")
@page
async def experiment_runs(
    request: Request, experiment_id: str, principal: Person, session: Db
) -> Response:
    """The run list alone, for filtering without reloading the page."""
    return render(
        request,
        "run_list.html",
        experiment_id=experiment_id,
        runs=await _run_list(request, principal, session, experiment_id),
        config_names=await _config_names(principal, session),
    )


@router.get("/runs/{run_id}")
@page
async def run_detail(request: Request, run_id: str, principal: Person, session: Db) -> Response:
    detail = await runs.run(run_id, principal, session)
    events = (
        await runs.events(detail.attempts[-1].id, principal, session) if detail.attempts else []
    )
    flagged: dict[str, list[str]] = {}
    for score in detail.scores:
        if score.status == "fail":
            for event_id in score.evidence.get("event_ids", []):
                flagged.setdefault(event_id, []).append(score.reason_code or score.dimension)
    return render(
        request,
        "run.html",
        principal=principal,
        run=detail,
        events=events,
        flagged=flagged,
        config_names=await _config_names(principal, session),
    )


@router.get("/comparisons")
@page
async def comparison_list(request: Request, principal: Person, session: Db) -> Response:
    finished = [
        e
        for e in await experiments.experiments(principal, session)
        if e.status in ("completed", "closed_partial", "cancelled")
    ]
    return render(
        request,
        "comparisons.html",
        principal=principal,
        comparisons=await comparisons.comparisons(principal, session),
        finished=finished,
        config_names=await _config_names(principal, session),
        can_change=role_at_least(principal.role, models.Role.MEMBER),
    )


@router.get("/comparisons/{comparison_id}")
@page
async def comparison_detail(
    request: Request, comparison_id: str, principal: Person, session: Db
) -> Response:
    return render(
        request,
        "comparison.html",
        principal=principal,
        comparison=await comparisons.comparison(comparison_id, principal, session),
    )


@router.get("/settings")
@page
async def settings(request: Request, principal: Person, session: Db) -> Response:
    is_owner = principal.role == models.Role.OWNER
    configs = await agent_configs.configs(principal, session)
    return render(
        request,
        "settings.html",
        principal=principal,
        is_owner=is_owner,
        can_change=role_at_least(principal.role, models.Role.MEMBER),
        members=await workspaces.members(principal, session),
        tokens=await api_tokens.list_tokens(principal, session) if is_owner else [],
        keys=await provider_credentials.credentials(principal, session) if is_owner else [],
        signing=await signing_secret.info(principal, session),
        endpoints=[c for c in configs if c.adapter_type == models.AdapterType.HTTP],
    )


def register_pages(app: Any) -> None:
    async def sign_in_first(request: Request, exc: Exception) -> Response:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    app.add_exception_handler(SignInRequiredError, sign_in_first)
    app.include_router(router)
