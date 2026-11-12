import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.budget.pricing import Price, cost_micros
from app.ingest.trace_format import Span, Trace
from app.providers.contract import Usage

# Turns a checked trace into what the platform stores about a run: totals for the run, and
# one row per span. Nothing here touches the database.


def fingerprint(config: dict[str, Any]) -> str:
    """The agent version's identity: any change to the config makes a new version."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class SpanRow:
    span_id: str
    parent_span_id: str | None
    kind: str
    name: str
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    input: Any
    output: Any
    error: str | None
    attributes: dict[str, Any]


@dataclass
class NormalizedRun:
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    environment: str | None
    session_id: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0
    # False when a model span had no price or no token counts, so cost_micros is a floor.
    cost_known: bool = True
    spans: list[SpanRow] = field(default_factory=list)


def _ms(start: datetime, end: datetime) -> int:
    return round((end - start).total_seconds() * 1000)


def _usage(span: Span) -> Usage:
    return Usage(
        input_tokens=span.input_tokens,
        cached_input_tokens=span.cached_input_tokens,
        output_tokens=span.output_tokens,
        other_tokens=span.other_tokens,
    )


def _tag(trace: Trace, name: str) -> str | None:
    value = trace.tags.get(name)
    return None if value is None else str(value)


def normalize(trace: Trace, prices: Mapping[tuple[str, str], Price]) -> NormalizedRun:
    root = trace.root
    run = NormalizedRun(
        started_at=root.start,
        ended_at=root.end,
        duration_ms=_ms(root.start, root.end),
        environment=_tag(trace, "environment"),
        session_id=_tag(trace, "session"),
    )
    for span in trace.spans:
        attributes = dict(span.attributes)
        if span.kind == "model":
            usage = _usage(span)
            price = prices.get((span.provider or "", span.model or ""))
            cost = cost_micros(usage, price) if price is not None else None
            run.input_tokens += (span.input_tokens or 0) + (span.cached_input_tokens or 0)
            run.output_tokens += (span.output_tokens or 0) + (span.other_tokens or 0)
            if cost is None:
                run.cost_known = False
            else:
                run.cost_micros += cost
            attributes.update(
                provider=span.provider,
                model=span.model,
                input_tokens=span.input_tokens,
                cached_input_tokens=span.cached_input_tokens,
                output_tokens=span.output_tokens,
                other_tokens=span.other_tokens,
                time_to_first_token_ms=span.time_to_first_token_ms,
                cost_micros=cost,
            )
        run.spans.append(
            SpanRow(
                span_id=span.id,
                parent_span_id=span.parent_id,
                kind=span.kind,
                name=span.name,
                started_at=span.start,
                ended_at=span.end,
                duration_ms=_ms(span.start, span.end),
                input=span.input,
                output=span.output,
                error=span.error,
                attributes=attributes,
            )
        )
    return run
