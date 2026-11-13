from app.metrics.comparison import MetricChange
from app.metrics.health import Health
from app.web.health_view import tiles

HEALTH = Health(
    runs=150,
    runs_scored=148,
    runs_decided=148,
    success_rate=0.82,
    rule_violation_rate=0.05,
    consistency=0.68,
    duration_p50_ms=6200,
    duration_p95_ms=14800,
    cost_per_success_micros=5100,
    cost_exact=False,
    incomplete=0,
    pending_calibration=False,
)


def _metric(
    name: str, delta: float, real: bool | None, worse: bool, base: float = 1
) -> MetricChange:
    return MetricChange(
        name=name,
        baseline=base,
        candidate=base + delta,
        delta=delta,
        low=None,
        high=None,
        real=real,
        worse=worse,
    )


def test_six_tiles_with_values_and_honest_verdicts() -> None:
    found = tiles(
        HEALTH,
        [
            _metric("success_rate", -0.06, True, True),
            _metric("duration_p50_ms", -400, False, False, base=6600),
            _metric("cost_per_success_micros", 200, None, True, base=4900),
        ],
    )
    by_label = {t.label.split(" ·")[0]: t for t in found}
    assert len(found) == 6
    success = by_label["Success rate"]
    assert (success.value, success.change, success.verdict, success.tone) == (
        "82%",
        "-6.0 pts",
        "likely real",
        "bad",
    )
    run_time = by_label["Run time"]
    assert (run_time.value, run_time.change, run_time.verdict, run_time.tone) == (
        "6.2 s / 14.8 s",
        "-400 ms",
        "could be noise",
        "neutral",
    )
    cost = by_label["Cost per successful run"]
    assert cost.value == "$0.0051 (some runs unpriced)"
    assert (cost.change, cost.verdict) == ("+4%", "too few runs to tell")
    assert by_label["Runs scored"].value == "148 of 150"
    assert by_label["Rule violations"].change is None
