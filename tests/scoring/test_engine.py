from app.scoring.definitions import ScorerDefinition
from app.scoring.engine import Scorer, score, verdict
from tests.scoring.golden import golden, view


def _scorer(key: str, check: str, **fields: object) -> Scorer:
    return Scorer(f"scr_{key}", ScorerDefinition(key=key, check=check, **fields))  # type: ignore[arg-type]


def test_a_scorer_that_does_not_apply_leaves_no_score() -> None:
    run = view(golden("status"), "status")
    scorers = [
        _scorer("only_gold", "run_completed", applies_when={"tags": {"tier": "gold"}}),
        _scorer("only_refunds", "run_completed", applies_when={"tools_used": ["issue_refund"]}),
        _scorer("live_only", "run_completed", applies_when={"has_expected": False}),
        _scorer("ran", "run_completed"),
    ]
    assert [r.key for r in score(run, scorers)] == ["ran"]


def test_a_crashing_scorer_records_an_error_not_a_verdict() -> None:
    trace = golden("refund")
    run = view(trace, "refund")
    broken = run.__class__(
        run.tags,
        {"tools": [{"name": "get_order", "parameters": {"type": "no-such-type"}}]},
        run.expected,
        run.has_expected,
        run.spans,
    )
    scorers = [_scorer("args", "valid_tool_arguments"), _scorer("ran", "run_completed")]
    results = score(broken, scorers)
    [error] = [r for r in results if r.error]
    assert (error.key, error.value, error.passed) == ("args", None, None)
    assert verdict(scorers, results).status == "incomplete"
    assert verdict(scorers, results).errored == ("args",)


def test_the_verdict_and_quality_score() -> None:
    run = view(golden("refund"), "refund")
    hard = _scorer("ran", "run_completed")
    soft_pass = _scorer("polite", "reply_excludes", params={"text": "stupid"}, severity="soft")
    soft_fail = _scorer(
        "thanks", "reply_contains", params={"text": "thank you"}, severity="soft", threshold=0.5
    )
    steps = _scorer("tools", "tool_errors", severity="soft")
    scorers = [hard, soft_pass, soft_fail, steps]
    result = verdict(scorers, score(run, scorers))
    # A soft scorer under its threshold fails the run; quality averages the soft scores.
    assert (result.status, result.failed) == ("failed", ("thanks",))
    assert result.quality == (1.0 + 0.0 + 1.0) / 3
    assert verdict([hard], score(run, [hard])).status == "passed"
    assert verdict([hard], []).status == "not_scored"
