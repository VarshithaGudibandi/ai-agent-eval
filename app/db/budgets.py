from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, update

from app.db import models
from app.db.ids import new_id
from app.db.scope import WorkspaceScope


class BudgetLimits(BaseModel):
    """Caps for one experiment. Leave a cap out to track that dimension without limiting it."""

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    money_micros: int | None = Field(default=None, ge=0)
    tokens: int | None = Field(default=None, ge=0)
    requests: int | None = Field(default=None, ge=0)


class LimitBelowUsageError(ValueError):
    pass


async def create_budget(
    scope: WorkspaceScope, experiment_id: str, limits: BudgetLimits
) -> models.Budget:
    budget = models.Budget(
        id=new_id("bud"),
        workspace_id=scope.workspace_id,
        experiment_id=experiment_id,
        currency=limits.currency,
        money_limit_micros=limits.money_micros,
        tokens_limit=limits.tokens,
        requests_limit=limits.requests,
    )
    scope.add(budget)
    await scope.flush()
    return budget


async def budget_for_experiment(scope: WorkspaceScope, experiment_id: str) -> models.Budget:
    budget = await scope.first(
        scope.query(models.Budget).where(models.Budget.experiment_id == experiment_id)
    )
    if budget is None:
        raise LookupError(experiment_id)
    return budget


async def try_reserve(
    scope: WorkspaceScope,
    budget_id: str,
    *,
    money_micros: int,
    tokens: int,
    requests: int,
    attempt_id: str | None,
) -> models.SpendEntry | None:
    """Reserve against every cap in one conditional update, or reserve nothing.

    Postgres serializes updates to the same row, so two workers can never both take the
    last of a budget: the second one re-checks the condition against the first one's total.
    """
    b = models.Budget
    reserved = await scope.session.scalar(
        update(b)
        .where(
            b.workspace_id == scope.workspace_id,
            b.id == budget_id,
            or_(
                b.money_limit_micros.is_(None),
                b.money_used_micros + money_micros <= b.money_limit_micros,
            ),
            or_(b.tokens_limit.is_(None), b.tokens_used + tokens <= b.tokens_limit),
            or_(b.requests_limit.is_(None), b.requests_used + requests <= b.requests_limit),
        )
        .values(
            money_used_micros=b.money_used_micros + money_micros,
            tokens_used=b.tokens_used + tokens,
            requests_used=b.requests_used + requests,
        )
        .returning(b.id)
    )
    if reserved is None:
        return None
    entry = models.SpendEntry(
        id=new_id("spend"),
        workspace_id=scope.workspace_id,
        budget_id=budget_id,
        attempt_id=attempt_id,
        reservation_id=None,
        kind=models.LedgerKind.RESERVATION,
        money_micros=money_micros,
        tokens=tokens,
        requests=requests,
        cost_known=False,
    )
    scope.add(entry)
    await scope.flush()
    return entry


async def settle(
    scope: WorkspaceScope,
    reservation: models.SpendEntry,
    *,
    money_micros: int | None,
    tokens: int | None,
    requests: int,
) -> models.SpendEntry:
    """Replace a reservation with what the call really used.

    Unknown amounts keep the reserved estimate, because unknown cost is never zero.
    """
    known = money_micros is not None and tokens is not None
    delta_money = (
        money_micros if money_micros is not None else reservation.money_micros
    ) - reservation.money_micros
    delta_tokens = (tokens if tokens is not None else reservation.tokens) - reservation.tokens
    delta_requests = requests - reservation.requests
    b = models.Budget
    await scope.session.execute(
        update(b)
        .where(b.workspace_id == scope.workspace_id, b.id == reservation.budget_id)
        .values(
            money_used_micros=b.money_used_micros + delta_money,
            tokens_used=b.tokens_used + delta_tokens,
            requests_used=b.requests_used + delta_requests,
        )
    )
    entry = models.SpendEntry(
        id=new_id("spend"),
        workspace_id=scope.workspace_id,
        budget_id=reservation.budget_id,
        attempt_id=reservation.attempt_id,
        reservation_id=reservation.id,
        kind=models.LedgerKind.RECONCILIATION,
        money_micros=delta_money,
        tokens=delta_tokens,
        requests=delta_requests,
        cost_known=known,
    )
    scope.add(entry)
    await scope.flush()
    return entry


async def mark_warned(scope: WorkspaceScope, budget_id: str) -> bool:
    """Set the warning time once. Returns True only for the call that set it."""
    b = models.Budget
    marked = await scope.session.scalar(
        update(b)
        .where(b.workspace_id == scope.workspace_id, b.id == budget_id, b.warned_at.is_(None))
        .values(warned_at=datetime.now(UTC))
        .returning(b.id)
    )
    return marked is not None


async def raise_limits(scope: WorkspaceScope, budget: models.Budget, limits: BudgetLimits) -> None:
    if limits.currency != budget.currency:
        raise LimitBelowUsageError("the budget currency cannot change")
    pairs = [
        (limits.money_micros, budget.money_used_micros),
        (limits.tokens, budget.tokens_used),
        (limits.requests, budget.requests_used),
    ]
    if any(limit is not None and limit < used for limit, used in pairs):
        raise LimitBelowUsageError("a limit cannot be set below what is already used")
    budget.money_limit_micros = limits.money_micros
    budget.tokens_limit = limits.tokens
    budget.requests_limit = limits.requests
    budget.warned_at = None


def has_headroom(budget: models.Budget) -> bool:
    pairs = [
        (budget.money_used_micros, budget.money_limit_micros),
        (budget.tokens_used, budget.tokens_limit),
        (budget.requests_used, budget.requests_limit),
    ]
    return all(limit is None or used < limit for used, limit in pairs)
