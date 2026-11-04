from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import error_body
from app.auth.api_tokens import resolve_api_token
from app.auth.principal import Principal, role_at_least
from app.auth.sessions import csrf_valid, resolve_session
from app.db.models import Role
from app.db.scope import WorkspaceScope
from app.providers.key_vault import KeyVault, MasterKeyMissingError, key_vault_from_settings
from app.settings import Settings

SESSION_COOKIE = "agent_eval_session"
CSRF_HEADER = "x-csrf-token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _fail(code: int, error: str) -> HTTPException:
    return HTTPException(status_code=code, detail=error_body(error))


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def key_vault(settings: Annotated[Settings, Depends(get_settings_dep)]) -> KeyVault:
    try:
        return key_vault_from_settings(settings)
    except MasterKeyMissingError as exc:
        raise HTTPException(
            status_code=503, detail={"error": "master_key_missing", "message": str(exc)}
        ) from None


Vault = Annotated[KeyVault, Depends(key_vault)]


async def db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One transaction per request. It commits only if the route finishes without error."""
    async with request.app.state.sessionmaker.begin() as session:
        yield session


# Function scope commits before the response is sent. With the default, a client could get
# 201 for a write that is not committed yet, or that then fails to commit.
Db = Annotated[AsyncSession, Depends(db_session, scope="function")]


async def current_principal(request: Request, session: Db) -> Principal:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        principal = await resolve_api_token(session, authorization[7:].strip())
        if principal is None:
            raise _fail(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
        return principal
    cookie = request.cookies.get(SESSION_COOKIE)
    resolved = await resolve_session(session, cookie) if cookie else None
    if resolved is None:
        raise _fail(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
    principal, row = resolved
    # A cookie is sent by the browser automatically, so a changing request must also
    # prove it came from our own page by echoing the CSRF token.
    if request.method not in _SAFE_METHODS and not csrf_valid(
        row, request.headers.get(CSRF_HEADER)
    ):
        raise _fail(status.HTTP_403_FORBIDDEN, "csrf_failed")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require_role(minimum: Role) -> Callable[..., Coroutine[Any, Any, Principal]]:
    async def check(principal: CurrentPrincipal) -> Principal:
        if not role_at_least(principal.role, minimum):
            raise _fail(status.HTTP_403_FORBIDDEN, "forbidden")
        return principal

    return check


Viewer = Annotated[Principal, Depends(require_role(Role.VIEWER))]
Member = Annotated[Principal, Depends(require_role(Role.MEMBER))]
Owner = Annotated[Principal, Depends(require_role(Role.OWNER))]


def scope_for(session: AsyncSession, principal: Principal) -> WorkspaceScope:
    return WorkspaceScope(session, principal.workspace_id)
