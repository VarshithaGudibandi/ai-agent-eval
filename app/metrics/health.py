from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel

# The six health metrics, worked out the same way everywhere: for an experiment, for a
# judge batch and for a day of live traffic.


@dataclass(frozen=True)
class RunOutcome:
    """What the metrics need from one run and its newest verdict."""

    run_id: str
    # The dataset case, for build-loop runs. Live runs have none.
    case_key: str | None
    # passed, failed, incomplete or not_scored; None while the run waits for scoring.
    status: str | None
    duration_ms: int
    cost_micros: int
    cost_known: bool
    # Hard deterministic scorers that failed: rule violations.
    hard_failures: tuple[str, ...] = ()
    # A judge question was left out because it is not calibrated yet.
    pending_calibration: bool = False

    @property
    def decided(self) -> bool:
        return self.status in ("passed", "failed")

    @property
    def passed(self) -> bool:
        return self.status == "passed"


class Health(BaseModel):
    runs: int
    runs_scored: int
    # Runs with a pass or fail verdict, the base of the rates below. Incomplete runs (a
    # scorer could not run) and runs no scorer applied to are left out.
    runs_decided: int
    success_rate: float | None
    rule_violation_rate: float | None
    # Cases that passed on every repetition, out of cases with a decided run.
    consistency: float | None
    duration_p50_ms: int | None
    duration_p95_ms: int | None
    cost_per_success_micros: int | None
    # False when some run had no price, so the cost is a floor.
    cost_exact: bool
    incomplete: int
    pending_calibration: bool


def percentile(values: Sequence[int], p: int) -> int | None:
    """Nearest-rank percentile, so it is always a value that really occurred."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-p * len(ordered) // 100))
    return ordered[rank - 1]


def consistency(runs: Sequence[RunOutcome]) -> float | None:
    cases: dict[str, list[bool]] = {}
    for run in runs:
        if run.case_key is not None and run.decided:
            cases.setdefault(run.case_key, []).append(run.passed)
    if not cases:
        return None
    return sum(all(results) for results in cases.values()) / len(cases)


def cost_per_success(runs: Sequence[RunOutcome]) -> int | None:
    """None when nothing passed, or when no run had a price: unknown is not free."""
    passed = sum(r.passed for r in runs)
    if not passed or not any(r.cost_known for r in runs):
        return None
    return round(sum(r.cost_micros for r in runs) / passed)


def health(runs: Sequence[RunOutcome]) -> Health:
    scored = [r for r in runs if r.status is not None]
    decided = [r for r in scored if r.decided]
    durations = [r.duration_ms for r in runs]
    return Health(
        runs=len(runs),
        runs_scored=len(scored),
        runs_decided=len(decided),
        success_rate=sum(r.passed for r in decided) / len(decided) if decided else None,
        rule_violation_rate=(
            sum(bool(r.hard_failures) for r in decided) / len(decided) if decided else None
        ),
        consistency=consistency(runs),
        duration_p50_ms=percentile(durations, 50),
        duration_p95_ms=percentile(durations, 95),
        cost_per_success_micros=cost_per_success(runs),
        cost_exact=all(r.cost_known for r in runs),
        incomplete=sum(r.status == "incomplete" for r in scored),
        pending_calibration=any(r.pending_calibration for r in runs),
    )
