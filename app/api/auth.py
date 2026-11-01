from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from app.api.dependencies import SESSION_COOKIE, CurrentPrincipal, Db, get_settings_dep
from app.auth.passwords import MIN_PASSWORD_LENGTH
from app.auth.sessions import end_session, start_session
from app.auth.users import authenticate, setup_first_owner
from app.db.models import Role
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
    response.set_cookie(
        SESSION_COOKIE,
        new.token,
        max_age=int(ttl.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )
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
