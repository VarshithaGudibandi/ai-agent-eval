from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.api.dependencies import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    CurrentPrincipal,
    Db,
    get_settings_dep,
)
from app.auth.passwords import MIN_PASSWORD_LENGTH
from app.auth.sessions import NewSession, end_session, start_session
from app.auth.users import authenticate, setup_first_owner
from app.db.models import Role
from app.demo.seed import demo_viewer
from app.settings import Settings

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class SetupRequest(BaseModel):
    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)
    workspace_name: str = Field(min_length=1, max_length=200)


class SetupResponse(BaseModel):
    user_id: str
    workspace_id: str


class LoginRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)
    workspace_id: str | None = None


class LoginResponse(BaseModel):
    user_id: str
    workspace_id: str
    role: Role
    csrf_token: str


@router.post("/setup", status_code=status.HTTP_201_CREATED)
async def setup(body: SetupRequest, session: Db) -> SetupResponse:
    """Create the first user and make them owner of the first workspace. Works only once."""
    user, workspace = await setup_first_owner(
        session, body.email, body.password, body.workspace_name
    )
    return SetupResponse(user_id=user.id, workspace_id=workspace.id)


def _signed_in(response: Response, new: NewSession, ttl: timedelta, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        new.token,
        max_age=int(ttl.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        new.csrf_token,
        max_age=int(ttl.total_seconds()),
        httponly=False,
        samesite="strict",
        secure=settings.session_cookie_secure,
        path="/",
    )


@router.post("/login")
async def login(
    body: LoginRequest,
    response: Response,
    session: Db,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> LoginResponse:
    """Start a session. The cookie carries it; changing requests must echo the CSRF token."""
    user = await authenticate(session, body.email, body.password)
    ttl = timedelta(hours=settings.session_ttl_hours)
    new = await start_session(session, user, body.workspace_id, ttl)
    _signed_in(response, new, ttl, settings)
    return LoginResponse(
        user_id=user.id,
        workspace_id=new.row.workspace_id,
        role=new.role,
        csrf_token=new.csrf_token,
    )


@router.post("/demo")
async def demo(
    response: Response,
    session: Db,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> LoginResponse:
    """Sign in to the demo workspace as a viewer, with no account. Only when DEMO_MODE is on
    and the demo is loaded."""
    user = await demo_viewer(session) if settings.demo_mode else None
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "there is no demo on this server")
    ttl = timedelta(hours=settings.session_ttl_hours)
    new = await start_session(session, user, None, ttl)
    _signed_in(response, new, ttl, settings)
    return LoginResponse(
        user_id=user.id,
        workspace_id=new.row.workspace_id,
        role=new.role,
        csrf_token=new.csrf_token,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(principal: CurrentPrincipal, response: Response, session: Db) -> None:
    """End the current session."""
    if principal.session_id is not None:
        await end_session(session, principal.session_id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
