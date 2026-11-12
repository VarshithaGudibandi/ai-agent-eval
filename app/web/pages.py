from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.api import api_tokens, provider_credentials, workspaces
from app.api.dependencies import SESSION_COOKIE, Db
from app.auth.principal import Principal
from app.auth.sessions import resolve_session
from app.db import models
from app.web import formatting

# Server-rendered pages. Each page reads through the same handlers as the JSON API, so a page
# can never show what the API would not, and every change a page makes is a call to /api/v1
# from the browser.

TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name("templates"))
STATIC_DIR = Path(__file__).with_name("static")
formatting.register(TEMPLATES.env)

# Pages run no inline script and load nothing from elsewhere, so injected markup in trace
# content could not run even if escaping were ever missed.
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


@router.get("/login")
async def login(request: Request) -> HTMLResponse:
    return render(request, "login.html")


@router.get("/")
async def home() -> RedirectResponse:
    return RedirectResponse("/settings", status_code=303)


@router.get("/settings")
async def settings(request: Request, principal: Person, session: Db) -> Response:
    is_owner = principal.role == models.Role.OWNER
    return render(
        request,
        "settings.html",
        principal=principal,
        is_owner=is_owner,
        members=await workspaces.members(principal, session),
        tokens=await api_tokens.list_tokens(principal, session) if is_owner else [],
        keys=await provider_credentials.credentials(principal, session) if is_owner else [],
    )


def register_pages(app: Any) -> None:
    async def sign_in_first(request: Request, exc: Exception) -> Response:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    app.add_exception_handler(SignInRequiredError, sign_in_first)
    app.include_router(router)
