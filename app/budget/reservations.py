import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.attempts import require_live
from app.db.audit import record_audit
from app.db.budgets import mark_warned, settle, try_reserve
from app.db.experiments import pause_for_budget
from app.db.scope import WorkspaceScope

logger = logging.getLogger(__name__)

WARNING_SHARE = 0.8


class BudgetExhaustedError(Exception):
    """The next call cannot fit in what is left. The experiment has been paused."""


class BudgetGuard:
    """Reserves budget before each model call and settles it after."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        workspace_id: str,
        experiment_id: str,
        budget_id: str,
        attempt_id: str,
        lease_token: str | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._workspace_id = workspace_id
        self._experiment_id = experiment_id
        self._budget_id = budget_id
        self._attempt_id = attempt_id
        self._lease_token = lease_token

    async def reserve(self, money_micros: int, tokens: int) -> models.SpendEntry:
        async with self._sessionmaker.begin() as session:
            # A worker that lost its lease must not spend the budget on a call it cannot use.
            if self._lease_token is not None:
                await require_live(session, self._workspace_id, self._attempt_id, self._lease_token)
            entry = await try_reserve(
                WorkspaceScope(session, self._workspace_id),
                self._budget_id,
                money_micros=money_micros,
                tokens=tokens,
                requests=1,
                attempt_id=self._attempt_id,
            )
        if entry is None:
            await self._pause("a call did not fit in the remaining budget")
            raise BudgetExhaustedError(self._experiment_id)
        return entry

    async def settle(
        self,
        session: AsyncSession,
        reservation: models.SpendEntry,
        *,
        money_micros: int | None,
        tokens: int | None,
        requests: int,
    ) -> None:
        """Settle inside the caller's transaction, so a call and its cost commit together."""
        scope = WorkspaceScope(session, self._workspace_id)
        await settle(
            scope, reservation, money_micros=money_micros, tokens=tokens, requests=requests
        )
        budget = await scope.require(models.Budget, self._budget_id)
        await session.refresh(budget)
        if _share_used(budget) >= WARNING_SHARE and await mark_warned(scope, budget.id):
            record_audit(
                scope,
                models.ActorType.SYSTEM,
                "budget",
                "budget.warning",
                "experiment",
                self._experiment_id,
                {"share_used": round(_share_used(budget), 3)},
            )
            logger.warning(
                "experiment passed 80% of a budget", extra={"experiment_id": self._experiment_id}
            )
        # Pause before the next call fails rather than after: if what is left cannot cover
        # another call like this one, no new attempt should start.
        if not _fits(budget, reservation):
            await _pause_in(scope, self._experiment_id, "remaining budget is below one more call")

    async def _pause(self, reason: str) -> None:
        async with self._sessionmaker.begin() as session:
            await _pause_in(
                WorkspaceScope(session, self._workspace_id), self._experiment_id, reason
            )


async def _pause_in(scope: WorkspaceScope, experiment_id: str, reason: str) -> None:
    if await pause_for_budget(scope, experiment_id):
        record_audit(
            scope,
            models.ActorType.SYSTEM,
            "budget",
            "experiment.paused",
            "experiment",
            experiment_id,
            {"reason": reason},
        )


def _share_used(budget: models.Budget) -> float:
    pairs = [
        (budget.money_used_micros, budget.money_limit_micros),
        (budget.tokens_used, budget.tokens_limit),
        (budget.requests_used, budget.requests_limit),
    ]
    return max(
        (used / limit if limit else 1.0 for used, limit in pairs if limit is not None), default=0.0
    )


def _fits(budget: models.Budget, like: models.SpendEntry) -> bool:
    pairs = [
        (budget.money_used_micros, budget.money_limit_micros, like.money_micros),
        (budget.tokens_used, budget.tokens_limit, like.tokens),
        (budget.requests_used, budget.requests_limit, like.requests),
    ]
    return all(limit is None or used + need <= limit for used, limit, need in pairs)
