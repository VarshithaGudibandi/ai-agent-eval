import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence

from pydantic import BaseModel

from app.db.reports import RunFacts

# The scorecard of one agent config in one experiment. Every rate carries its numerator and
# denominator, so a reader can see what was counted. Agent-behavior rates use scorable runs
# only: a run the platform failed to finish says nothing about the agent.

SCORABLE = ("pass", "fail", "partial")


class Rate(BaseModel):
    numerator: int
    denominator: int
    value: float | None

    @classmethod
    def of(cls, numerator: int, denominator: int) -> "Rate":
        return cls(
            numerator=numerator,
            denominator=denominator,
            value=numerator / denominator if denominator else None,
        )


class Latency(BaseModel):
    count: int
    p50_ms: int | None
    p95_ms: int | None

    @classmethod
    def of(cls, values: Iterable[int | None]) -> "Latency":
        known = sorted(v for v in values if v is not None)
        return cls(count=len(known), p50_ms=percentile(known, 50), p95_ms=percentile(known, 95))


class Cost(BaseModel):
    """What the platform metered for this config's model calls, retries included."""

    money_micros: int
    # False when some model call had no price, so money_micros is a floor, not a total.
    money_known: bool
    tokens: int
    requests: int
    money_per_success_micros: int | None
    # What external agents said they used. Unverified, never charged to a budget.
    self_reported: dict[str, int]


class CategoryResult(BaseModel):
    category: str
    planned: int
    verified_success: Rate


class ReasonCount(BaseModel):
    layer: str
    reason_code: str
    runs: int


class Scorecard(BaseModel):
    agent_config_id: str
    planned: int
    scorable: int
    passed: int
    verified_success: Rate
    strict_success: Rate
    coverage: Rate
    unsafe_attempt_rate: Rate
    executed_violation_rate: Rate
    tool_schema_validity: Rate
    tool_call_precision: Rate
    required_call_recall: Rate
    passed_all_repetitions: Rate
    queue_latency: Latency
    execution_latency: Latency
    cost: Cost
    categories: list[CategoryResult]
    failure_reasons: list[ReasonCount]
    resolved_models: list[str]


def percentile(values: Sequence[int], p: int) -> int | None:
    """Nearest rank, so the answer is always a value that was really observed."""
    if not values:
        return None
    rank = max(1, math.ceil(p / 100 * len(values)))
    return values[rank - 1]


def is_scorable(run: RunFacts) -> bool:
    return run.evaluation_status in SCORABLE


def _violations(run: RunFacts) -> list[bool]:
    """Whether each policy violation in the run went through (True) or was blocked."""
    found: list[bool] = []
    for score in run.scores:
        if score.layer != "policy" or score.status != "fail":
            continue
        listed = score.evidence.get("violations")
        if isinstance(listed, list) and listed:
            found.extend(bool(v.get("executed")) for v in listed if isinstance(v, dict))
        else:
            found.append(bool(score.evidence.get("executed")))
    return found


def _policy_applies(run: RunFacts) -> bool:
    return any(s.layer == "policy" and s.applicable for s in run.scores)


def scorecard(config_id: str, runs: Sequence[RunFacts]) -> Scorecard:
    scorable = [r for r in runs if is_scorable(r)]
    passed = [r for r in scorable if r.evaluation_status == "pass"]

    policy_runs = [r for r in scorable if _policy_applies(r)]
    unsafe = [r for r in policy_runs if not all(_violations(r))]
    executed = [r for r in policy_runs if any(_violations(r))]

    matched = relevant = required = 0
    for run in scorable:
        for score in run.scores:
            if score.dimension != "required_calls" or not score.applicable:
                continue
            evidence = score.evidence
            matched += int(evidence.get("matched", 0))
            required += int(evidence.get("required", 0))
            relevant += int(evidence.get("relevant_calls", 0))

    by_task: defaultdict[str, list[RunFacts]] = defaultdict(list)
    for run in runs:
        by_task[run.task_key].append(run)
    all_passed = sum(
        1 for task_runs in by_task.values() if all(r.evaluation_status == "pass" for r in task_runs)
    )

    by_category: defaultdict[str, list[RunFacts]] = defaultdict(list)
    for run in runs:
        by_category[run.category].append(run)

    reasons: Counter[tuple[str, str]] = Counter()
    for run in scorable:
        if run.evaluation_status == "pass":
            continue
        reasons.update(
            {(s.layer, s.reason_code) for s in run.scores if s.status == "fail" and s.reason_code}
        )

    money = sum(r.cost_micros for r in runs)
    reported: Counter[str] = Counter()
    for run in runs:
        reported.update(run.self_reported)

    return Scorecard(
        agent_config_id=config_id,
        planned=len(runs),
        scorable=len(scorable),
        passed=len(passed),
        verified_success=Rate.of(len(passed), len(scorable)),
        strict_success=Rate.of(len(passed), len(runs)),
        coverage=Rate.of(len(scorable), len(runs)),
        unsafe_attempt_rate=Rate.of(len(unsafe), len(policy_runs)),
        executed_violation_rate=Rate.of(len(executed), len(policy_runs)),
        tool_schema_validity=Rate.of(
            sum(r.tool_calls_attempted - r.tool_calls_invalid for r in scorable),
            sum(r.tool_calls_attempted for r in scorable),
        ),
        tool_call_precision=Rate.of(matched, relevant),
        required_call_recall=Rate.of(matched, required),
        passed_all_repetitions=Rate.of(all_passed, len(by_task)),
        queue_latency=Latency.of(r.queue_ms for r in runs),
        execution_latency=Latency.of(r.duration_ms for r in runs if r.attempt_id),
        cost=Cost(
            money_micros=money,
            money_known=all(r.cost_known for r in runs),
            tokens=sum(r.model_tokens for r in runs),
            requests=sum(r.model_requests for r in runs),
            money_per_success_micros=round(money / len(passed)) if passed else None,
            self_reported=dict(sorted(reported.items())),
        ),
        categories=[
            CategoryResult(
                category=category,
                planned=len(category_runs),
                verified_success=Rate.of(
                    sum(1 for r in category_runs if r.evaluation_status == "pass"),
                    sum(1 for r in category_runs if is_scorable(r)),
                ),
            )
            for category, category_runs in sorted(by_category.items())
        ],
        failure_reasons=[
            ReasonCount(layer=layer, reason_code=code, runs=count)
            for (layer, code), count in sorted(reasons.items(), key=lambda i: (-i[1], i[0]))
        ],
        resolved_models=sorted(set().union(*(r.resolved_models for r in runs))),
    )


def scorecards(config_ids: Sequence[str], runs: Sequence[RunFacts]) -> list[Scorecard]:
    """One scorecard per config, in the experiment's config order."""
    return [scorecard(c, [r for r in runs if r.agent_config_id == c]) for c in config_ids]
