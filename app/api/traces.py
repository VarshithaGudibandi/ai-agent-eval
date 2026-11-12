from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, ValidationError

from app.api.dependencies import Db, Member, get_settings_dep, scope_for
from app.ingest.service import BatchInvalidError, BatchTooLargeError, ingest
from app.settings import Settings

router = APIRouter(prefix="/api/v1/traces", tags=["traces"])


class TraceBatch(BaseModel):
    # Each trace is checked on its own, so one bad trace does not sink the batch.
    traces: list[dict[str, Any]] = Field(min_length=1, max_length=100)


class RejectedOut(BaseModel):
    trace_id: str | None
    error: str


class IngestOut(BaseModel):
    accepted: list[str]
    duplicates: list[str]
    rejected: list[RejectedOut]


async def _body(request: Request, limit: int) -> bytes:
    # Read under a cap instead of letting the framework buffer any size it is sent.
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise BatchTooLargeError(f"a batch may be at most {limit} bytes")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise BatchTooLargeError(f"a batch may be at most {limit} bytes")
    return bytes(body)


@router.post(
    "",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": TraceBatch.model_json_schema()}},
        }
    },
)
async def post_traces(
    request: Request,
    principal: Member,
    session: Db,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> IngestOut:
    """Ingest a batch of traces. Returns which were accepted, already known, or rejected."""
    try:
        batch = TraceBatch.model_validate_json(await _body(request, settings.ingest_max_bytes))
    except ValidationError as exc:
        raise BatchInvalidError(exc.errors()[0]["msg"]) from None
    result = await ingest(scope_for(session, principal), batch.traces)
    return IngestOut(
        accepted=result.accepted,
        duplicates=result.duplicates,
        rejected=[RejectedOut(trace_id=r.trace_id, error=r.error) for r in result.rejected],
    )
