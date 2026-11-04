from typing import Any

from app.db.reports import RunFacts, ScoreFacts
from app.reports.metrics import percentile, scorecard


def _score(layer: str, dimension: str, status: str, **evidence: Any) -> ScoreFacts:
    return ScoreFacts(
        evaluator=f"e_{layer}",
        version="1.0.0",
        layer=layer,
        dimension=dimension,
        applicable=status != "not_applicable",
        status=status,
        reason_code="REASON_" + dimension.upper() if status == "fail" else None,
        evidence=evidence,
    )


def _run(task: str, status: str, *scores: ScoreFacts, repetition: int = 1, **kw: Any) -> RunFacts:
    return RunFacts(
        run_id=f"run_{task}_{repetition}",
        agent_config_id="cfg",
        task_version_id=f"tv_{task}",
        task_key=task,
        category="refund" if task < "m" else "lookup",
        repetition=repetition,
        execution_status="completed" if status != "pending" else "infrastructure_error",
        evaluation_status=status,
        attempt_id=f"att_{task}_{repetition}",
        attempts=1,
        scores=list(scores),
        **kw,
    )


POLICY_PASS = _score("policy", "refund_approval", "pass")


def _blocked() -> ScoreFacts:
    return _score("policy", "refund_approval", "fail", violations=[{"executed": False}])


def _executed() -> ScoreFacts:
    return _score("policy", "refund_limit", "fail", executed=True, violations=[{"executed": True}])


def test_success_rates_use_their_own_denominators() -> None:
    runs = [
        _run("a", "pass", POLICY_PASS),
        _run("b", "fail", _blocked()),
        _run("c", "partial", _executed()),
        # Unscored: counts against strict success and coverage, never against the agent.
        _run("d", "pending"),
    ]
    card = scorecard("cfg", runs)
    assert (card.planned, card.scorable, card.passed) == (4, 3, 1)
    assert (card.verified_success.numerator, card.verified_success.denominator) == (1, 3)
    assert (card.strict_success.numerator, card.strict_success.denominator) == (1, 4)
    assert card.coverage.value == 0.75
    assert (card.unsafe_attempt_rate.numerator, card.unsafe_attempt_rate.denominator) == (1, 3)
    assert (card.executed_violation_rate.numerator, card.executed_violation_rate.denominator) == (
        1,
        3,
    )


def test_hard_violations_are_counted_even_on_a_passing_run() -> None:
    card = scorecard("cfg", [_run("a", "pass", _executed())])
    assert card.executed_violation_rate.numerator == 1


def test_a_run_with_both_kinds_of_violation_counts_in_both_rates() -> None:
    both = _score(
        "policy", "refund_approval", "fail", violations=[{"executed": True}, {"executed": False}]
    )
    card = scorecard("cfg", [_run("a", "fail", both)])
    assert card.unsafe_attempt_rate.numerator == card.executed_violation_rate.numerator == 1


def test_call_quality_counts_calls_not_runs() -> None:
    required = _score(
        "trajectory", "required_calls", "fail", matched=2, required=4, relevant_calls=5
    )
    runs = [
        _run("a", "fail", required, tool_calls_attempted=6, tool_calls_invalid=2),
        _run("b", "pass", tool_calls_attempted=4, tool_calls_invalid=0),
        _run("c", "pending", tool_calls_attempted=9, tool_calls_invalid=9),
    ]
    card = scorecard("cfg", runs)
    assert (card.tool_schema_validity.numerator, card.tool_schema_validity.denominator) == (8, 10)
    assert (card.required_call_recall.numerator, card.required_call_recall.denominator) == (2, 4)
    assert (card.tool_call_precision.numerator, card.tool_call_precision.denominator) == (2, 5)


def test_a_task_passes_all_repetitions_only_if_every_one_passed() -> None:
    runs = [
        _run("a", "pass", repetition=1),
        _run("a", "pass", repetition=2),
        _run("b", "pass", repetition=1),
        _run("b", "fail", repetition=2),
        _run("c", "pass", repetition=1),
        _run("c", "pending", repetition=2),
    ]
    card = scorecard("cfg", runs)
    assert (card.passed_all_repetitions.numerator, card.passed_all_repetitions.denominator) == (
        1,
        3,
    )


def test_cost_per_success_includes_failed_runs_and_flags_unknown_prices() -> None:
    runs = [
        _run("a", "pass", cost_micros=300, model_requests=2, model_tokens=10),
        _run("b", "fail", cost_micros=100, model_requests=1, model_tokens=5, cost_known=False),
        _run("c", "pass", self_reported={"input_tokens": 7}),
        _run("d", "fail", self_reported={"input_tokens": 3, "output_tokens": 1}),
    ]
    cost = scorecard("cfg", runs).cost
    assert (cost.money_micros, cost.requests, cost.tokens) == (400, 3, 15)
    assert cost.money_per_success_micros == 200
    assert cost.money_known is False
    assert cost.self_reported == {"input_tokens": 10, "output_tokens": 1}


def test_no_passes_means_no_cost_per_success() -> None:
    assert (
        scorecard("cfg", [_run("a", "fail", cost_micros=5)]).cost.money_per_success_micros is None
    )


def test_failure_reasons_are_ranked_by_runs() -> None:
    runs = [
        _run("a", "fail", _blocked()),
        _run("b", "fail", _blocked(), _executed()),
        _run("c", "partial", _executed()),
        _run("d", "fail", _executed()),
    ]
    reasons = scorecard("cfg", runs).failure_reasons
    assert [(r.reason_code, r.runs) for r in reasons] == [
        ("REASON_REFUND_LIMIT", 3),
        ("REASON_REFUND_APPROVAL", 2),
    ]


def test_categories_split_the_results() -> None:
    card = scorecard("cfg", [_run("a", "pass"), _run("b", "fail"), _run("x", "pass")])
    assert [(c.category, c.verified_success.value) for c in card.categories] == [
        ("lookup", 1.0),
        ("refund", 0.5),
    ]


def test_latency_uses_observed_values_by_nearest_rank() -> None:
    assert percentile([], 50) is None
    assert percentile([10], 95) == 10
    values = list(range(1, 101))
    assert (percentile(values, 50), percentile(values, 95)) == (50, 95)
    runs = [
        _run(t, "pass", queue_ms=q, duration_ms=d) for t, q, d in [("a", 5, 100), ("b", 1, 300)]
    ]
    card = scorecard("cfg", runs)
    assert (card.queue_latency.p50_ms, card.queue_latency.p95_ms) == (1, 5)
    assert (card.execution_latency.p50_ms, card.execution_latency.count) == (100, 2)


def test_an_empty_config_has_no_rates() -> None:
    card = scorecard("cfg", [])
    assert card.verified_success.value is None
    assert card.cost.money_per_success_micros is None
