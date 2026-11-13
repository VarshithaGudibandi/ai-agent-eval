import hashlib
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from statistics import NormalDist
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.metrics.health import (
    Health,
    RunOutcome,
    consistency,
    cost_per_success,
    health,
    percentile,
)

# Compares two sets of runs: two experiments, paired case by case, or two windows of live
# traffic, unpaired. Every change comes with an interval, so noise is not read as a
# regression, and the gates turn the result into ship or hold.

RESAMPLES = 2000
CONFIDENCE = 0.95
MIN_CASES = 5
MIN_RUNS = 30
# The z value for CONFIDENCE, for the quick interval.
Z = NormalDist().inv_cdf(1 - (1 - CONFIDENCE) / 2)

CaseOutcome = Literal["worse", "better", "same"]
Decision = Literal["ship", "hold", "insufficient_data"]


class Gates(BaseModel):
    """When a candidate is worse enough to hold back. Stored per agent."""

    model_config = ConfigDict(extra="forbid")

    # Success rate may fall by at most this much, as a share (0.05 is 5 points).
    max_success_drop: float = Field(default=0.05, ge=0, le=1)
    # Cost per successful run may rise by at most this share; None means no limit.
    max_cost_increase: float | None = Field(default=0.2, ge=0)
    # Hold when a hard rule fails that never failed in the baseline.
    hold_on_new_hard_failure: bool = True


@dataclass(frozen=True)
class SideFacts:
    """What the fairness check needs to know about one side."""

    label: str
    dataset_version_id: str | None
    eval_spec_ids: frozenset[str]
    finished: bool
    repetitions: int | None


class MetricChange(BaseModel):
    name: str
    baseline: float | None
    candidate: float | None
    delta: float | None
    low: float | None
    high: float | None
    # The interval excludes zero. None when there is not enough data to say.
    real: bool | None
    # The change goes the bad way for this metric.
    worse: bool


class CaseResult(BaseModel):
    case_key: str
    baseline_pass_rate: float
    candidate_pass_rate: float
    outcome: CaseOutcome


class ComparisonResult(BaseModel):
    paired: bool
    # Problems that make the comparison unfair. Blocking ones mean no decision.
    fairness: list[str]
    baseline: Health
    candidate: Health
    metrics: list[MetricChange]
    cases: list[CaseResult]
    new_hard_failures: list[str]
    decision: Decision
    reasons: list[str]


def seed_for(*parts: str) -> int:
    """The same inputs always give the same intervals, so a result can be checked again."""
    return int.from_bytes(hashlib.sha256("|".join(parts).encode()).digest()[:4], "big")


def fairness(baseline: SideFacts, candidate: SideFacts) -> tuple[list[str], bool]:
    """What makes the comparison unfair, and whether any of it blocks a decision."""
    notes, blocking = [], False
    if baseline.dataset_version_id != candidate.dataset_version_id:
        notes.append("the two sides ran on different dataset versions")
        blocking = True
    if baseline.eval_spec_ids != candidate.eval_spec_ids:
        notes.append("the two sides were graded by different eval spec versions; rescore one")
        blocking = True
    for side in (baseline, candidate):
        if not side.finished:
            notes.append(f"the {side.label} is not finished, so runs may be missing")
    if baseline.repetitions != candidate.repetitions:
        notes.append(
            f"repetitions differ ({baseline.repetitions} and {candidate.repetitions}), "
            "so consistency is not like for like"
        )
    return notes, blocking


Statistic = Callable[[Sequence[RunOutcome]], float | None]


def _success(runs: Sequence[RunOutcome]) -> float | None:
    decided = [r for r in runs if r.decided]
    return sum(r.passed for r in decided) / len(decided) if decided else None


def _violations(runs: Sequence[RunOutcome]) -> float | None:
    decided = [r for r in runs if r.decided]
    return sum(bool(r.hard_failures) for r in decided) / len(decided) if decided else None


def _p50(runs: Sequence[RunOutcome]) -> float | None:
    value = percentile([r.duration_ms for r in runs], 50)
    return None if value is None else float(value)


def _cost(runs: Sequence[RunOutcome]) -> float | None:
    value = cost_per_success(runs)
    return None if value is None else float(value)


# Name, statistic, and whether a higher value is better.
METRICS: list[tuple[str, Statistic, bool]] = [
    ("success_rate", _success, True),
    ("rule_violation_rate", _violations, False),
    ("consistency", consistency, True),
    ("duration_p50_ms", _p50, False),
    ("cost_per_success_micros", _cost, False),
]


def _interval(
    stat: Statistic,
    groups: list[tuple[list[RunOutcome], list[RunOutcome]]] | None,
    baseline: Sequence[RunOutcome],
    candidate: Sequence[RunOutcome],
    seed: int,
) -> tuple[float | None, float | None]:
    """A bootstrap interval for candidate minus baseline. Paired: whole cases are resampled,
    each with both sides' runs, so case difficulty cancels out. Unpaired: each side's runs
    are resampled on their own."""
    rng = random.Random(seed)  # noqa: S311 - statistics, not security
    deltas = []
    for _ in range(RESAMPLES):
        if groups is not None:
            picked = rng.choices(groups, k=len(groups))
            base = [r for b, _ in picked for r in b]
            cand = [r for _, c in picked for r in c]
        else:
            base = rng.choices(list(baseline), k=len(baseline))
            cand = rng.choices(list(candidate), k=len(candidate))
        b, c = stat(base), stat(cand)
        if b is not None and c is not None:
            deltas.append(c - b)
    if len(deltas) < RESAMPLES // 2:
        return None, None
    deltas.sort()
    tail = (1 - CONFIDENCE) / 2
    return deltas[int(tail * len(deltas))], deltas[
        min(len(deltas) - 1, int((1 - tail) * len(deltas)))
    ]


# The metrics that are a share of decided runs, which the quick interval can cover.
PROPORTIONS: dict[str, Callable[[RunOutcome], bool]] = {
    "success_rate": lambda r: r.passed,
    "rule_violation_rate": lambda r: bool(r.hard_failures),
}


def _quick_interval(
    name: str, baseline: Sequence[RunOutcome], candidate: Sequence[RunOutcome]
) -> tuple[float | None, float | None]:
    """A normal-approximation interval for a difference of two shares, with no resampling,
    so a page can afford it on thousands of live runs. Other metrics get none, and with fewer
    than MIN_RUNS decided runs on a side the approximation is not trusted at all."""
    pick = PROPORTIONS.get(name)
    b = [pick(r) for r in baseline if r.decided] if pick else []
    c = [pick(r) for r in candidate if r.decided] if pick else []
    if min(len(b), len(c)) < MIN_RUNS:
        return None, None
    pb, pc = sum(b) / len(b), sum(c) / len(c)
    spread = Z * (pb * (1 - pb) / len(b) + pc * (1 - pc) / len(c)) ** 0.5
    return pc - pb - spread, pc - pb + spread


def _cases(
    baseline: Sequence[RunOutcome], candidate: Sequence[RunOutcome]
) -> tuple[list[CaseResult], list[tuple[list[RunOutcome], list[RunOutcome]]]]:
    by_case: dict[str, tuple[list[RunOutcome], list[RunOutcome]]] = {}
    for side, runs in ((0, baseline), (1, candidate)):
        for run in runs:
            if run.case_key is not None:
                by_case.setdefault(run.case_key, ([], []))[side].append(run)
    results, groups = [], []
    for key, (base, cand) in sorted(by_case.items()):
        b = [r for r in base if r.decided]
        c = [r for r in cand if r.decided]
        if not b or not c:
            continue
        groups.append((base, cand))
        b_rate = sum(r.passed for r in b) / len(b)
        c_rate = sum(r.passed for r in c) / len(c)
        outcome: CaseOutcome = (
            "worse" if c_rate < b_rate else "better" if c_rate > b_rate else "same"
        )
        results.append(
            CaseResult(
                case_key=key, baseline_pass_rate=b_rate, candidate_pass_rate=c_rate, outcome=outcome
            )
        )
    order = {"worse": 0, "better": 1, "same": 2}
    results.sort(
        key=lambda r: (order[r.outcome], r.candidate_pass_rate - r.baseline_pass_rate, r.case_key)
    )
    return results, groups


def compare(
    baseline: Sequence[RunOutcome],
    candidate: Sequence[RunOutcome],
    baseline_facts: SideFacts,
    candidate_facts: SideFacts,
    gates: Gates,
    paired: bool,
    seed: int,
    bootstrap: bool = True,
) -> ComparisonResult:
    """Compare two sides. Without bootstrap, only success and rule violations get an
    interval (see _quick_interval), which is enough for the gates to decide."""
    notes, blocking = fairness(baseline_facts, candidate_facts)
    cases, groups = _cases(baseline, candidate) if paired else ([], [])
    metrics = []
    for name, stat, higher_is_better in METRICS:
        b, c = stat(baseline), stat(candidate)
        low, high = (
            _interval(stat, groups if paired else None, baseline, candidate, seed)
            if bootstrap
            else _quick_interval(name, baseline, candidate)
        )
        delta = None if b is None or c is None else c - b
        metrics.append(
            MetricChange(
                name=name,
                baseline=b,
                candidate=c,
                delta=delta,
                low=low,
                high=high,
                real=None if low is None or high is None else not (low <= 0 <= high),
                worse=delta is not None and (delta < 0 if higher_is_better else delta > 0),
            )
        )
    base_failures = {k for r in baseline for k in r.hard_failures}
    new_failures = sorted({k for r in candidate for k in r.hard_failures} - base_failures)
    enough = len(groups) >= MIN_CASES if paired else min(len(baseline), len(candidate)) >= MIN_RUNS
    if not enough:
        notes.append(
            f"too little data: fewer than {MIN_CASES} paired cases"
            if paired
            else f"too little data: fewer than {MIN_RUNS} runs on a side"
        )
    decision, reasons = decide(metrics, new_failures, gates, blocking, enough)
    return ComparisonResult(
        paired=paired,
        fairness=notes,
        baseline=health(baseline),
        candidate=health(candidate),
        metrics=metrics,
        cases=cases,
        new_hard_failures=new_failures,
        decision=decision,
        reasons=reasons,
    )


def decide(
    metrics: list[MetricChange],
    new_failures: list[str],
    gates: Gates,
    blocking: bool,
    enough: bool,
) -> tuple[Decision, list[str]]:
    """Hold when a gate trips on a real change, or a new hard failure appears.

    An unfair comparison decides nothing. With too little data only a new hard failure can
    hold: it is a fact, not an estimate.
    """
    if blocking:
        return "insufficient_data", ["the two sides cannot be compared fairly"]
    reasons = []
    if gates.hold_on_new_hard_failure and new_failures:
        reasons.append(f"new hard failures: {', '.join(new_failures)}")
    by_name = {m.name: m for m in metrics}
    success = by_name["success_rate"]
    if success.real and success.delta is not None and -success.delta > gates.max_success_drop:
        reasons.append(
            f"success rate down {-success.delta * 100:.1f} points, more than the "
            f"{gates.max_success_drop * 100:.1f} allowed"
        )
    cost = by_name["cost_per_success_micros"]
    if (
        gates.max_cost_increase is not None
        and cost.real
        and cost.baseline
        and cost.candidate is not None
        and cost.candidate / cost.baseline - 1 > gates.max_cost_increase
    ):
        rise = cost.candidate / cost.baseline - 1
        reasons.append(
            f"cost per successful run up {rise * 100:.0f}%, more than the "
            f"{gates.max_cost_increase * 100:.0f}% allowed"
        )
    if reasons:
        return "hold", reasons
    if not enough:
        return "insufficient_data", ["too little data to tell a change from noise"]
    return "ship", ["no gate tripped"]
