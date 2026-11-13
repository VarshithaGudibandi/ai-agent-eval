from datetime import UTC, datetime, timedelta

from app.api.runs import ScoreOut, SpanOut
from app.web.trace_view import selected_span, span_rows, step_of, tokens_per_second

START = datetime(2026, 11, 12, 10, tzinfo=UTC)


def _span(span_id: str, parent: str | None, start_ms: int, ms: int, **extra: object) -> SpanOut:
    values: dict[str, object] = {
        "span_id": span_id,
        "parent_span_id": parent,
        "kind": "tool",
        "name": span_id,
        "started_at": START + timedelta(milliseconds=start_ms),
        "ended_at": START + timedelta(milliseconds=start_ms + ms),
        "duration_ms": ms,
        "input": None,
        "output": None,
        "error": None,
        "attributes": {},
    }
    values.update(extra)
    return SpanOut.model_validate(values)


def _score(span_id: str | None, passed: bool) -> ScoreOut:
    return ScoreOut(
        scorer_key="k",
        scorer_version=1,
        check="tool_errors",
        severity="hard",
        span_id=span_id,
        value=1.0 if passed else 0.0,
        passed=passed,
        reason="",
        evidence=[],
        error=None,
    )


SPANS = [
    _span("late", "root", 600, 400),
    _span("root", None, 0, 1000),
    _span("child", "early", 150, 1),
    _span("early", "root", 100, 200),
]


def test_spans_come_in_tree_order_with_their_place_in_time() -> None:
    rows = span_rows(SPANS)
    assert [(r.span.span_id, r.depth) for r in rows] == [
        ("root", 0),
        ("early", 1),
        ("child", 2),
        ("late", 1),
    ]
    by_id = {r.span.span_id: r for r in rows}
    assert (by_id["root"].offset, by_id["root"].width) == (0, 100)
    assert (by_id["late"].offset, by_id["late"].width, by_id["late"].at_ms) == (60, 40, 600)
    # A one-millisecond span still shows.
    assert by_id["child"].width >= 0.6


def test_the_step_shown_is_the_one_asked_for_else_the_one_that_failed() -> None:
    rows = span_rows([*SPANS[:3], _span("early", "root", 100, 200, error="boom")])
    assert selected_span(rows, [], "late").span.span_id == "late"
    assert selected_span(rows, [_score("child", False)], None).span.span_id == "child"
    assert selected_span(rows, [_score("child", True)], "missing").span.span_id == "early"
    assert selected_span(span_rows(SPANS), [], None).span.span_id == "root"


def test_speed_needs_a_first_token_time() -> None:
    model = _span(
        "m", None, 0, 800, attributes={"time_to_first_token_ms": 300, "output_tokens": 50}
    )
    assert tokens_per_second(model) == 100
    assert tokens_per_second(_span("m", None, 0, 800, attributes={"output_tokens": 50})) is None


def test_a_score_on_the_whole_run_points_at_the_step_its_evidence_names() -> None:
    rows = span_rows(SPANS)
    run_level = _score(None, False).model_copy(update={"evidence": ["not-a-span", "late"]})
    assert step_of(run_level, {r.span.span_id for r in rows}) == "late"
    assert selected_span(rows, [run_level], None).span.span_id == "late"
    assert step_of(_score(None, True), {"root"}) is None
