from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.auth.errors import (
    InvalidCredentialsError,
    InviteInvalidError,
    NotAMemberError,
    RoleTooHighError,
    SetupAlreadyDoneError,
    UserRequiredError,
)
from app.db.experiments import ExperimentRejectedError
from app.db.scope import NotFoundError

# Another workspace's rows answer exactly like missing rows, so ids cannot be probed.
_ERRORS: dict[type[Exception], tuple[int, str]] = {
    NotFoundError: (404, "not_found"),
    NotAMemberError: (404, "not_found"),
    InvalidCredentialsError: (401, "invalid_credentials"),
    RoleTooHighError: (403, "role_too_high"),
    UserRequiredError: (403, "user_required"),
    SetupAlreadyDoneError: (409, "setup_already_done"),
    InviteInvalidError: (400, "invite_invalid"),
    ExperimentRejectedError: (422, "experiment_rejected"),
}


def error_body(code: str, message: str | None = None) -> dict[str, str]:
    return {"error": code, **({"message": message} if message else {})}


def register_error_handlers(app: FastAPI) -> None:
    for error_type, (status, code) in _ERRORS.items():

        async def handler(
            request: Request, exc: Exception, status: int = status, code: str = code
        ) -> JSONResponse:
            message = str(exc) if isinstance(exc, ExperimentRejectedError) else None
            return JSONResponse(status_code=status, content=error_body(code, message))

        app.add_exception_handler(error_type, handler)
