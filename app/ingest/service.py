import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.budget.pricing import Price, latest_price
from app.db import models
from app.db.scope import WorkspaceScope
from app.db.traces import agent_version, save_run, store_raw
from app.ingest.redact import redact_value
from app.ingest.trace_format import Trace
from app.traces.normalize import fingerprint, normalize

logger = logging.getLogger(__name__)


class BatchTooLargeError(Exception):
    """The request body is over the ingest size limit."""


class BatchInvalidError(Exception):
    """The request is not a batch of traces at all, so nothing in it can be judged."""


@dataclass(frozen=True)
class Rejected:
    trace_id: str | None
    error: str


@dataclass
class IngestResult:
    accepted: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)


def _problem(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:3]:
        where = ".".join(str(p) for p in error["loc"])
        parts.append(f"{where}: {error['msg']}" if where else error["msg"])
    return "; ".join(parts)


async def _prices(
    scope: WorkspaceScope, trace: Trace, known: dict[tuple[str, str], Price | None]
) -> dict[tuple[str, str], Price]:
    for span in trace.spans:
        if span.kind != "model" or span.provider is None or span.model is None:
            continue
        key = (span.provider, span.model)
        if key not in known:
            row = await latest_price(scope, *key)
            known[key] = None if row is None else Price.of(row)
    return {key: price for key, price in known.items() if price is not None}


async def ingest(scope: WorkspaceScope, documents: list[dict[str, Any]]) -> IngestResult:
    """Store each trace as received, then build its run, spans, cost and agent version.

    Each trace stands alone: one that fails validation is rejected with the reason, and the
    rest of the batch still goes in. A trace already stored is reported as a duplicate, so
    the SDK can resend a batch safely.
    """
    result = IngestResult()
    known_prices: dict[tuple[str, str], Price | None] = {}
    for document in documents:
        # Masking comes first, so a secret can reach neither storage nor an error message.
        masked = redact_value(document)
        claimed_id = masked.get("trace_id") if isinstance(masked, dict) else None
        try:
            trace = Trace.model_validate(masked)
        except ValidationError as exc:
            result.rejected.append(
                Rejected(claimed_id if isinstance(claimed_id, str) else None, _problem(exc))
            )
            continue
        row = await store_raw(scope, trace.trace_id, trace.agent, trace.model_dump(mode="json"))
        if row is None:
            result.duplicates.append(trace.trace_id)
            continue
        result.accepted.append(trace.trace_id)
        try:
            # A savepoint: if building the run fails, the raw trace is still kept.
            async with scope.session.begin_nested():
                version = await agent_version(
                    scope, trace.agent, fingerprint(trace.config), trace.config, trace.label
                )
                run = normalize(trace, await _prices(scope, trace, known_prices))
                await save_run(scope, row, version, trace.tags, run)
        except Exception as exc:
            logger.exception("could not normalize a trace", extra={"trace_id": trace.trace_id})
            row.status = models.TraceStatus.FAILED
            row.error = f"{type(exc).__name__}: {exc}"[:500]
    return result
