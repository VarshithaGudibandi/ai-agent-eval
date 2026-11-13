from app.metrics.health import RunOutcome, health, percentile


def _run(case: str | None, status: str | None, **fields: object) -> RunOutcome:
    values: dict[str, object] = {
        "run_id": f"r{id(fields)}",
        "case_key": case,
        "status": status,
        "duration_ms": 1000,
        "cost_micros": 100,
        "cost_known": True,
    }
    values.update(fields)
    return RunOutcome(**values)  # type: ignore[arg-type]


def test_the_six_metrics() -> None:
    runs = [
        _run("a", "passed", duration_ms=800),
        _run("a", "passed", duration_ms=900),
        _run("b", "passed"),
        _run("b", "failed", hard_failures=("refund_after_lookup",), duration_ms=5000),
        _run("c", "incomplete", cost_known=False),
        _run("c", None),
    ]
    h = health(runs)
    assert (h.runs, h.runs_scored, h.runs_decided, h.incomplete) == (6, 5, 4, 1)
    assert h.success_rate == 3 / 4
    assert h.rule_violation_rate == 1 / 4
    # Case a passed every time; b did not; c has no decided run, so it is left out.
    assert h.consistency == 1 / 2
    assert (h.duration_p50_ms, h.duration_p95_ms) == (1000, 5000)
    assert h.cost_per_success_micros == 200 and not h.cost_exact


def test_no_runs_and_live_runs_without_cases() -> None:
    empty = health([])
    assert (empty.success_rate, empty.consistency, empty.cost_per_success_micros) == (
        None,
        None,
        None,
    )
    live = health([_run(None, "passed"), _run(None, "failed")])
    assert (live.success_rate, live.consistency) == (0.5, None)
    assert percentile([5, 1, 3], 50) == 3
