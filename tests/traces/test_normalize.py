from app.budget.pricing import Price
from app.ingest.trace_format import Trace
from app.traces.normalize import fingerprint, normalize
from tests.ingest.sample_traces import CONFIG, EXPECTED_COST, PRICE, support_trace

PRICES = {
    ("gemini", "gemini-3.5-flash-lite"): Price(
        price_version_id="price_1",
        currency="USD",
        **{k: v for k, v in PRICE.items() if k.endswith("micros")},  # type: ignore[arg-type]
    )
}


def test_the_fingerprint_ignores_key_order_but_not_content() -> None:
    reordered = dict(reversed(list(CONFIG.items())))
    assert fingerprint(CONFIG) == fingerprint(reordered)
    assert fingerprint(CONFIG) != fingerprint({**CONFIG, "prompt": CONFIG["prompt"] + " "})
    assert len(fingerprint(CONFIG)) == 64


def test_totals_come_from_the_model_spans_and_time_from_the_root() -> None:
    run = normalize(Trace.model_validate(support_trace()), PRICES)
    assert (run.cost_micros, run.cost_known) == (EXPECTED_COST, True)
    assert (run.input_tokens, run.output_tokens) == (2700, 80)
    assert run.duration_ms == 2500
    assert (run.environment, run.session_id) == ("production", "sess-42")
    assert [s.span_id for s in run.spans] == ["root", "m1", "t1", "m2"]
    assert {s.span_id: s.duration_ms for s in run.spans}["t1"] == 200


def test_cost_is_unknown_without_a_price_or_without_token_counts() -> None:
    assert not normalize(Trace.model_validate(support_trace()), {}).cost_known
    trace = support_trace()
    del trace["spans"][3]["output_tokens"]  # type: ignore[index]
    run = normalize(Trace.model_validate(trace), PRICES)
    # The span with counts is still charged; the run's cost is a floor.
    assert (run.cost_micros, run.cost_known) == (1300, False)


def test_only_model_spans_get_model_attributes() -> None:
    run = normalize(Trace.model_validate(support_trace()), PRICES)
    by_id = {s.span_id: s for s in run.spans}
    assert by_id["m1"].attributes["cost_micros"] == 1300
    assert "cost_micros" not in by_id["t1"].attributes
