from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException

from app.web.pages import agents, login, runs, settings
from app.web.rendering import SignInRequiredError, render_error


def register_pages(app: FastAPI) -> None:
    async def sign_in_first(request: Request, exc: Exception) -> Response:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    async def unknown_page(request: Request, exc: Exception) -> Response:
        # The API keeps FastAPI's own JSON answer; a person gets a page.
        if request.url.path.startswith("/api/") or not isinstance(exc, HTTPException):
            return await http_exception_handler(request, exc)  # type: ignore[arg-type]
        return render_error(request, exc.status_code, "not_found", None)

    app.add_exception_handler(SignInRequiredError, sign_in_first)
    app.add_exception_handler(HTTPException, unknown_page)
    for module in (login, agents, runs, settings):
        app.include_router(module.router)
