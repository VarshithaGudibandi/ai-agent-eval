from app.budget.pricing import Price, cost_micros, estimate_input_tokens, worst_case
from app.providers.contract import Message, Usage

PRICE = Price(
    price_version_id="price_1",
    currency="USD",
    input_per_million_micros=100_000,
    cached_input_per_million_micros=25_000,
    output_per_million_micros=400_000,
    other_per_million_micros=400_000,
)


def test_cost_adds_each_token_kind_and_rounds_up() -> None:
    usage = Usage(input_tokens=1_000, cached_input_tokens=1_000, output_tokens=500, other_tokens=1)
    # 100 + 25 + 200 + 0.4 micros, rounded up
    assert cost_micros(usage, PRICE) == 326


def test_unknown_usage_has_unknown_cost_and_free_models_cost_nothing() -> None:
    assert cost_micros(Usage(), PRICE) is None
    assert cost_micros(Usage(input_tokens=10, output_tokens=10), None) == 0


def test_worst_case_prices_all_output_at_the_higher_rate() -> None:
    assert worst_case(PRICE, input_tokens=1_000, max_output_tokens=1_000) == (500, 2_000)
    assert worst_case(None, input_tokens=10, max_output_tokens=5) == (0, 15)


def test_input_estimate_grows_with_the_conversation() -> None:
    short = estimate_input_tokens([Message(role="user", content="hi")], [])
    long = estimate_input_tokens([Message(role="user", content="hi " * 1000)], [])
    assert long > short + 900
