from datetime import datetime

from fastapi import APIRouter, status
from pydantic import Field

from app.api.dependencies import Db, Owner, Viewer, scope_for
from app.budget.pricing import PriceInput, create_price_version, list_prices
from app.db import models
from app.db.audit import record_audit

router = APIRouter(prefix="/api/v1/price-versions", tags=["prices"])


class NewPriceRequest(PriceInput):
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=200)


class PriceOut(NewPriceRequest):
    id: str
    created_at: datetime

    @classmethod
    def of(cls, row: models.PriceVersion) -> "PriceOut":
        return cls(
            id=row.id,
            provider=row.provider,
            model=row.model,
            currency=row.currency,
            input_per_million_micros=row.input_per_million_micros,
            cached_input_per_million_micros=row.cached_input_per_million_micros,
            output_per_million_micros=row.output_per_million_micros,
            other_per_million_micros=row.other_per_million_micros,
            created_at=row.created_at,
        )


@router.post("", status_code=status.HTTP_201_CREATED)
async def new_price(body: NewPriceRequest, principal: Owner, session: Db) -> PriceOut:
    """Add a price version. The newest one for a model is used by experiments created later."""
    scope = scope_for(session, principal)
    row = await create_price_version(
        scope, body.provider, body.model, PriceInput.model_validate(body.model_dump())
    )
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "price.created",
        "price_version",
        row.id,
        {"provider": body.provider, "model": body.model},
    )
    await session.refresh(row)
    return PriceOut.of(row)


@router.get("")
async def prices(principal: Viewer, session: Db) -> list[PriceOut]:
    """Every price version in this workspace."""
    return [PriceOut.of(p) for p in await list_prices(scope_for(session, principal))]
