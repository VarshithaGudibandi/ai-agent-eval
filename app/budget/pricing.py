import json
import math

from pydantic import BaseModel, Field

from app.db import models
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.gateway.contract import PublicToolSchema
from app.providers.contract import Message, Usage


class PriceInput(BaseModel):
    """Per million tokens, in micro-units of the currency. 1 USD is 1,000,000 micros."""

    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    input_per_million_micros: int = Field(ge=0)
    cached_input_per_million_micros: int = Field(ge=0)
    output_per_million_micros: int = Field(ge=0)
    other_per_million_micros: int = Field(ge=0)


class Price(BaseModel):
    price_version_id: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    input_per_million_micros: int = Field(ge=0)
    cached_input_per_million_micros: int = Field(ge=0)
    output_per_million_micros: int = Field(ge=0)
    other_per_million_micros: int = Field(ge=0)

    @classmethod
    def of(cls, row: models.PriceVersion) -> "Price":
        return cls(
            price_version_id=row.id,
            currency=row.currency,
            input_per_million_micros=row.input_per_million_micros,
            cached_input_per_million_micros=row.cached_input_per_million_micros,
            output_per_million_micros=row.output_per_million_micros,
            other_per_million_micros=row.other_per_million_micros,
        )


def _micros(token_rate_products: int) -> int:
    # Round up, so the platform never reports less than was spent.
    return math.ceil(token_rate_products / 1_000_000)


def cost_micros(usage: Usage, price: Price | None) -> int | None:
    if not usage.known:
        return None
    if price is None:
        return 0
    return _micros(
        (usage.input_tokens or 0) * price.input_per_million_micros
        + (usage.cached_input_tokens or 0) * price.cached_input_per_million_micros
        + (usage.output_tokens or 0) * price.output_per_million_micros
        + (usage.other_tokens or 0) * price.other_per_million_micros
    )


def estimate_input_tokens(messages: list[Message], tools: list[PublicToolSchema]) -> int:
    # About three characters per token overestimates for English and JSON, which is the
    # safe side for a reservation.
    text = json.dumps([m.model_dump() for m in messages]) + json.dumps(
        [t.model_dump() for t in tools]
    )
    return len(text) // 3 + 16


def worst_case(price: Price | None, input_tokens: int, max_output_tokens: int) -> tuple[int, int]:
    """(money, tokens) the call could cost at most. Every input token priced as uncached."""
    tokens = input_tokens + max_output_tokens
    if price is None:
        return 0, tokens
    output_rate = max(price.output_per_million_micros, price.other_per_million_micros)
    money = _micros(input_tokens * price.input_per_million_micros + max_output_tokens * output_rate)
    return money, tokens


async def create_price_version(
    scope: WorkspaceScope, provider: str, model: str, price: PriceInput
) -> models.PriceVersion:
    row = models.PriceVersion(
        id=new_id("price"),
        workspace_id=scope.workspace_id,
        provider=provider,
        model=model,
        **price.model_dump(),
    )
    scope.add(row)
    await scope.flush()
    return row


async def latest_price(
    scope: WorkspaceScope, provider: str, model: str
) -> models.PriceVersion | None:
    return await scope.first(
        scope.query(models.PriceVersion)
        .where(models.PriceVersion.provider == provider, models.PriceVersion.model == model)
        .order_by(models.PriceVersion.created_at.desc(), models.PriceVersion.id.desc())
    )


async def list_prices(scope: WorkspaceScope) -> list[models.PriceVersion]:
    return await scope.all(
        scope.query(models.PriceVersion).order_by(
            models.PriceVersion.provider, models.PriceVersion.model, models.PriceVersion.created_at
        )
    )
