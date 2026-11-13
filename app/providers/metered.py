import asyncio
import logging
import random
import time

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.budget.pricing import Price, cost_micros, estimate_input_tokens, worst_case
from app.budget.reservations import BudgetGuard
from app.db import models
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.providers.contract import (
    ContentBlocked,
    Fatal,
    GenerationSettings,
    Message,
    ModelProvider,
    ModelResponse,
    ProviderError,
    PublicToolSchema,
    RateLimited,
    Retryable,
    Usage,
)
from app.providers.rate_limiter import RateLimiter, Sleep

logger = logging.getLogger(__name__)

# A provider that asks for a longer pause is out of quota for the day, not briefly busy.
# Waiting it out would hold the job's lease, and a worker slot, for hours.
MAX_WAIT_SECONDS = 60.0

_STATUS: dict[type[ProviderError], models.ModelCallStatus] = {
    RateLimited: models.ModelCallStatus.RATE_LIMITED,
    Retryable: models.ModelCallStatus.RETRYABLE_ERROR,
    Fatal: models.ModelCallStatus.FATAL_ERROR,
    ContentBlocked: models.ModelCallStatus.CONTENT_BLOCKED,
}


class MeteredModel:
    """A provider wrapped with everything a model call must go through.

    Each call reserves its worst-case cost, waits for the rate limiter, retries rate limits
    and transient errors with backoff, and records every try with its usage and cost.
    """

    def __init__(
        self,
        provider: ModelProvider,
        provider_name: str,
        *,
        price: Price | None,
        guard: BudgetGuard,
        limiter: RateLimiter | None,
        sessionmaker: async_sessionmaker[AsyncSession],
        workspace_id: str,
        timeout_seconds: float,
        max_retries: int,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._provider = provider
        self._provider_name = provider_name
        self._price = price
        self._guard = guard
        self._limiter = limiter
        self._sessionmaker = sessionmaker
        self._workspace_id = workspace_id
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._sleep = sleep

    async def generate(
        self, messages: list[Message], tools: list[PublicToolSchema], settings: GenerationSettings
    ) -> ModelResponse:
        money, tokens = worst_case(
            self._price, estimate_input_tokens(messages, tools), settings.max_output_tokens
        )
        for retry in range(self._max_retries + 1):
            reservation = await self._guard.reserve(money, tokens)
            if self._limiter is not None:
                await self._limiter.acquire(tokens)
            started = time.monotonic()
            try:
                response = await self._provider.generate(messages, tools, settings, self._timeout)
            except ProviderError as exc:
                await self._record_failure(reservation, settings, retry, started, exc)
                wait = _backoff(retry, exc)
                if (
                    not isinstance(exc, RateLimited | Retryable)
                    or retry == self._max_retries
                    or wait > MAX_WAIT_SECONDS
                ):
                    raise
                await self._sleep(wait)
                continue
            await self._record_success(reservation, settings, retry, started, response)
            return response
        raise AssertionError("unreachable")

    async def _record_success(
        self,
        reservation: models.SpendEntry,
        settings: GenerationSettings,
        retry: int,
        started: float,
        response: ModelResponse,
    ) -> None:
        cost = cost_micros(response.usage, self._price)
        tokens = response.usage.total() if response.usage.known else None
        await self._record(
            reservation,
            settings,
            retry,
            started,
            models.ModelCallStatus.OK,
            None,
            response.usage,
            response.resolved_model,
            cost,
            tokens,
            1,
        )

    async def _record_failure(
        self,
        reservation: models.SpendEntry,
        settings: GenerationSettings,
        retry: int,
        started: float,
        exc: ProviderError,
    ) -> None:
        status = _STATUS.get(type(exc), models.ModelCallStatus.RETRYABLE_ERROR)
        if isinstance(exc, RateLimited):
            # A rate-limited request is refused before any work, so it uses nothing.
            cost, tokens, requests = 0, 0, 0
        else:
            # The provider gave no usage, so the reserved estimate stands.
            cost, tokens, requests = None, None, 1
        await self._record(
            reservation,
            settings,
            retry,
            started,
            status,
            str(exc)[:500],
            Usage(),
            None,
            cost,
            tokens,
            requests,
        )

    async def _record(
        self,
        reservation: models.SpendEntry,
        settings: GenerationSettings,
        retry: int,
        started: float,
        status: models.ModelCallStatus,
        error: str | None,
        usage: Usage,
        resolved_model: str | None,
        cost: int | None,
        tokens: int | None,
        requests: int,
    ) -> None:
        call_id = new_id("mc")
        duration_ms = round((time.monotonic() - started) * 1000)
        async with self._sessionmaker.begin() as session:
            await self._guard.settle(
                session, reservation, money_micros=cost, tokens=tokens, requests=requests
            )
            WorkspaceScope(session, self._workspace_id).add(
                models.ModelCall(
                    id=call_id,
                    workspace_id=self._workspace_id,
                    reservation_id=reservation.id,
                    provider=self._provider_name,
                    requested_model=settings.model,
                    resolved_model=resolved_model,
                    status=status,
                    error=error,
                    retry_number=retry,
                    input_tokens=usage.input_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    output_tokens=usage.output_tokens,
                    other_tokens=usage.other_tokens,
                    usage_known=usage.known,
                    cost_micros=cost,
                    duration_ms=duration_ms,
                )
            )
        if status != models.ModelCallStatus.OK:
            logger.warning(
                "model call failed",
                extra={"provider": self._provider_name, "status": status, "retry": retry},
            )


def _backoff(retry: int, exc: ProviderError) -> float:
    if isinstance(exc, RateLimited) and exc.retry_after_seconds is not None:
        return exc.retry_after_seconds
    # Jitter keeps many workers that failed together from all retrying at the same moment.
    return min(30.0, 2.0**retry) * (0.5 + random.random() / 2)  # noqa: S311
