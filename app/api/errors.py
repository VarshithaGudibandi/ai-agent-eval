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
from app.auth.signing_secrets import SigningSecretRequiredError
from app.db.attempts import AttemptNotLiveError
from app.db.budgets import LimitBelowUsageError
from app.db.comparisons import SideUnclearError
from app.db.content import DatasetRejectedError
from app.db.experiments import ExperimentRejectedError, InvalidTransitionError
from app.db.run_tokens import InvalidRunTokenError, ResultAlreadyPostedError
from app.db.scope import NotFoundError
from app.execution.launch import PriceUnknownError, ProviderKeyRequiredError
from app.providers.registry import UnknownProviderError
from app.reports.comparison import IncompatibleError, NotFinishedError

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
    DatasetRejectedError: (422, "dataset_rejected"),
    ProviderKeyRequiredError: (409, "provider_key_required"),
    PriceUnknownError: (422, "price_unknown"),
    UnknownProviderError: (422, "provider_unknown"),
    InvalidTransitionError: (409, "invalid_transition"),
    LimitBelowUsageError: (422, "limit_below_usage"),
    SigningSecretRequiredError: (409, "signing_secret_required"),
    InvalidRunTokenError: (401, "invalid_run_token"),
    AttemptNotLiveError: (409, "attempt_not_live"),
    ResultAlreadyPostedError: (409, "result_already_posted"),
    SideUnclearError: (422, "comparison_side_unclear"),
    NotFinishedError: (409, "experiment_not_finished"),
}
_SHOW_MESSAGE = (
    SigningSecretRequiredError,
    ExperimentRejectedError,
    DatasetRejectedError,
    ProviderKeyRequiredError,
    PriceUnknownError,
    InvalidTransitionError,
    LimitBelowUsageError,
    SideUnclearError,
    NotFinishedError,
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

    async def incompatible(request: Request, exc: Exception) -> JSONResponse:
        # Callers need to know exactly which field to line up before comparing again.
        field = exc.field if isinstance(exc, IncompatibleError) else None
        return JSONResponse(
            status_code=422,
            content={**error_body("incompatible_experiments", str(exc)), "field": field},
        )

    app.add_exception_handler(IncompatibleError, incompatible)
