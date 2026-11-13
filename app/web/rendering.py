from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.dependencies import SESSION_COOKIE, Db
from app.auth.principal import Principal
from app.auth.sessions import resolve_session
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

ERROR_TITLES = {
    400: "That request did not work",
    403: "You cannot do that here",
    404: "Not found",
    409: "That cannot be done right now",
    422: "That request did not work",
}


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


def render_error(request: Request, status: int, code: str, message: str | None) -> HTMLResponse:
    """An API error raised while building a page, shown as a page. A missing row and another
    workspace's row look the same here too."""
    return render(
        request,
        "error.html",
        status_code=status,
        title=ERROR_TITLES.get(status, "Something went wrong"),
        code=code,
        message=message,
    )
