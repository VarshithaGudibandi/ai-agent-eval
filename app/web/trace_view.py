from dataclasses import dataclass

from app.api.runs import ScoreOut, SpanOut

# A run's span tree as rows of a waterfall: each span indented under its parent, with where
# it started and how long it took as shares of the whole run.

# A bar narrower than this would vanish, so very short spans still show.
MIN_WIDTH = 0.6


@dataclass(frozen=True)
class SpanRow:
    span: SpanOut
    depth: int
    # Start and length as percentages of the run, for the timing bar.
    offset: float
    width: float
    # Milliseconds from the start of the run.
    at_ms: int


def span_rows(spans: list[SpanOut]) -> list[SpanRow]:
    """Spans in tree order: each parent, then its children by start time."""
    if not spans:
        return []
    start = min(s.started_at for s in spans)
    end = max(s.ended_at for s in spans)
    total = max((end - start).total_seconds() * 1000, 1)
    ids = {s.span_id for s in spans}
    children: dict[str | None, list[SpanOut]] = {}
    for span in spans:
        parent = span.parent_span_id if span.parent_span_id in ids else None
        children.setdefault(parent, []).append(span)
    rows: list[SpanRow] = []

    def walk(parent: str | None, depth: int) -> None:
        for span in sorted(children.get(parent, []), key=lambda s: (s.started_at, s.span_id)):
            at = (span.started_at - start).total_seconds() * 1000
            offset = min(at / total * 100, 100 - MIN_WIDTH)
            width = max(span.duration_ms / total * 100, MIN_WIDTH)
            rows.append(SpanRow(span, depth, offset, min(width, 100 - offset), round(at)))
            walk(span.span_id, depth + 1)

    walk(None, 0)
    return rows


def selected_span(rows: list[SpanRow], scores: list[ScoreOut], asked: str | None) -> SpanRow:
    """The span to show: the one asked for, else the first a failing score points at, else
    the first with an error, else the root."""
    by_id = {r.span.span_id: r for r in rows}
    if asked in by_id:
        return by_id[asked]
    for score in scores:
        if score.passed is False and score.span_id in by_id:
            return by_id[score.span_id]
    return next((r for r in rows if r.span.error), rows[0])


def tokens_per_second(span: SpanOut) -> float | None:
    """Output speed after the first token, for a model span that reported both."""
    first = span.attributes.get("time_to_first_token_ms")
    out = span.attributes.get("output_tokens")
    if not isinstance(first, int) or not isinstance(out, int) or span.duration_ms <= first:
        return None
    return out / ((span.duration_ms - first) / 1000)
