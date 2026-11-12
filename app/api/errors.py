import math

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
from app.db.budgets import LimitBelowUsageError
from app.db.datasets import CaseKeysClashError
from app.db.experiments import IdempotencyKeyReusedError
from app.db.scope import NotFoundError
from app.ingest.service import BatchInvalidError, BatchTooLargeError, IngestRateLimitedError

# Another workspace's rows answer exactly like missing rows, so ids cannot be probed.
_ERRORS: dict[type[Exception], tuple[int, str]] = {
    NotFoundError: (404, "not_found"),
    NotAMemberError: (404, "not_found"),
    InvalidCredentialsError: (401, "invalid_credentials"),
    RoleTooHighError: (403, "role_too_high"),
    UserRequiredError: (403, "user_required"),
    SetupAlreadyDoneError: (409, "setup_already_done"),
    InviteInvalidError: (400, "invite_invalid"),
    LimitBelowUsageError: (422, "limit_below_usage"),
    BatchTooLargeError: (413, "batch_too_large"),
    BatchInvalidError: (422, "batch_invalid"),
    CaseKeysClashError: (422, "case_keys_clash"),
    IdempotencyKeyReusedError: (409, "idempotency_key_reused"),
}
_SHOW_MESSAGE = (
    LimitBelowUsageError,
    BatchTooLargeError,
    BatchInvalidError,
    CaseKeysClashError,
)


def error_body(code: str, message: str | None = None) -> dict[str, str]:
    return {"error": code, **({"message": message} if message else {})}


def register_error_handlers(app: FastAPI) -> None:
    for error_type, (status, code) in _ERRORS.items():

        async def handler(
            request: Request, exc: Exception, status: int = status, code: str = code
        ) -> JSONResponse:
            message = str(exc) if isinstance(exc, _SHOW_MESSAGE) else None
            return JSONResponse(status_code=status, content=error_body(code, message))

        app.add_exception_handler(error_type, handler)

    async def rate_limited(request: Request, exc: Exception) -> JSONResponse:
        # Retry-After tells a well-behaved sender exactly how long to back off.
        wait = exc.retry_after_seconds if isinstance(exc, IngestRateLimitedError) else 1.0
        return JSONResponse(
            status_code=429,
            content=error_body("rate_limited", str(exc)),
            headers={"retry-after": str(max(1, math.ceil(wait)))},
        )

    app.add_exception_handler(IngestRateLimitedError, rate_limited)
