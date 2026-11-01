from types import TracebackType
from typing import Any, Self

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.budget.pricing import Price
from app.budget.reservations import BudgetGuard
from app.db.scope import WorkspaceScope
from app.execution.launch import ProviderKeyRequiredError
from app.execution.trace_recorder import TraceRecorder
from app.providers.credentials import active_credential, decrypt_credential
from app.providers.key_vault import key_vault_from_settings
from app.providers.metered import MeteredModel
from app.providers.rate_limiter import RateLimiter
from app.providers.registry import ProviderRegistry
from app.settings import Settings


class AttemptModels:
    """Model clients for one attempt, each one metered against the experiment's budget."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        providers: ProviderRegistry,
        settings: Settings,
        workspace_id: str,
        experiment_id: str,
        budget_id: str,
        attempt_id: str,
        price: dict[str, Any] | None,
        recorder: TraceRecorder,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._providers = providers
        self._settings = settings
        self._workspace_id = workspace_id
        self._experiment_id = experiment_id
        self._budget_id = budget_id
        self._attempt_id = attempt_id
        self._price = Price.model_validate(price) if price else None
        self._recorder = recorder
        self._http = httpx.AsyncClient()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def client(self, provider: str) -> MeteredModel:
        entry = self._providers.get(provider)
        key: str | None = None
        limiter: RateLimiter | None = None
        if entry.needs_key:
            async with self._sessionmaker() as session:
                credential = await active_credential(
                    WorkspaceScope(session, self._workspace_id), provider
                )
            if credential is None:
                raise ProviderKeyRequiredError(provider)
            key = decrypt_credential(credential, key_vault_from_settings(self._settings))
            limiter = RateLimiter(self._sessionmaker, self._workspace_id, credential.id)
        return MeteredModel(
            entry.build(key, self._settings, self._http),
            provider,
            price=self._price,
            guard=BudgetGuard(
                self._sessionmaker,
                self._workspace_id,
                self._experiment_id,
                self._budget_id,
                self._attempt_id,
            ),
            limiter=limiter,
            recorder=self._recorder,
            sessionmaker=self._sessionmaker,
            workspace_id=self._workspace_id,
            attempt_id=self._attempt_id,
            timeout_seconds=self._settings.model_call_timeout_seconds,
            max_retries=self._settings.model_call_max_retries,
        )
