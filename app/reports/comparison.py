import hashlib
import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from app.reports.metrics import RunFacts, Scorecard, is_scorable, scorecard

# A paired comparison of two agent configs on the same tasks. Each side is one config in one
# experiment; both may be in the same experiment, which is the cleanest comparison because
# they ran on the same queue, with the same faults, at the same time.

RESAMPLES = 2000
CONFIDENCE = 0.95
# Below this many paired tasks a difference in success rate is easily chance.
MIN_TASKS = 30
FINISHED = ("completed", "closed_partial", "cancelled")

Outcome = Literal["loss", "win", "tie", "unscored"]
# Regressions first: they are what a reviewer has to look at.
_ORDER: dict[str, int] = {"loss": 0, "win": 1, "tie": 2, "unscored": 3}


@dataclass(frozen=True)
class Side:
    experiment_id: str
    agent_config_id: str
    config_name: str
    mode: str
    status: str
    dataset_version_id: str
    split: str
    environments: list[str]
    evaluators: list[dict[str, Any]]
    limits: dict[str, Any]


class IncompatibleError(ValueError):
    """The two sides differ in something that would make the comparison meaningless."""

    def __init__(self, field: str, baseline: Any, candidate: Any) -> None:
        super().__init__(f"{field} differs: baseline {baseline}, candidate {candidate}")
        self.field = field


class NotFinishedError(ValueError):
    pass


def check_compatible(baseline: Side, candidate: Side) -> None:
    """Refuse a comparison unless only the agent config differs. Names the first difference."""
    if (baseline.experiment_id, baseline.agent_config_id) == (
        candidate.experiment_id,
        candidate.agent_config_id,
    ):
        raise IncompatibleError("agent_config", "the same config", "the same config")
    for side in (baseline, candidate):
        if side.status not in FINISHED:
            raise NotFinishedError(f"experiment {side.experiment_id} is still {side.status}")
    for field in ("mode", "dataset_version_id", "split", "environments", "evaluators", "limits"):
        mine, theirs = getattr(baseline, field), getattr(candidate, field)
        if mine != theirs:
            raise IncompatibleError(field, mine, theirs)


class TaskSide(BaseModel):
    planned: int
    scorable: int
    passed: int
    pass_rate: float | None
    run_ids: list[str]
    # Why the scorable runs that did not pass failed, most common first.
    reason_codes: list[str]


class TaskResult(BaseModel):
    task_key: str
    category: str
    outcome: Outcome
    baseline: TaskSide
    candidate: TaskSide
    delta: float | None


class Interval(BaseModel):
    """Candidate minus baseline in mean per-task pass rate, with a bootstrap interval."""

    baseline: float | None
    candidate: float | None
    delta: float | None
    low: float | None
    high: float | None
    confidence: float
    resamples: int
    seed: int


class SideSummary(BaseModel):
    experiment_id: str
    agent_config_id: str
    config_name: str
    scorecard: Scorecard


class ComparisonResult(BaseModel):
    baseline: SideSummary
    candidate: SideSummary
    paired_tasks: int
    wins: int
    losses: int
    ties: int
    unscored: int
    success: Interval
    # Say so plainly whenever the difference may be chance.
    may_be_noise: bool
    notes: list[str]
    tasks: list[TaskResult]


def _task_side(runs: Sequence[RunFacts]) -> TaskSide:
    scorable = [r for r in runs if is_scorable(r)]
    passed = sum(1 for r in scorable if r.evaluation_status == "pass")
    reasons: defaultdict[str, int] = defaultdict(int)
    for run in scorable:
        if run.evaluation_status == "pass":
            continue
        for code in {s.reason_code for s in run.scores if s.status == "fail" and s.reason_code}:
            reasons[code] += 1
    return TaskSide(
        planned=len(runs),
        scorable=len(scorable),
        passed=passed,
        pass_rate=passed / len(scorable) if scorable else None,
        run_ids=[r.run_id for r in runs],
        reason_codes=[c for c, _ in sorted(reasons.items(), key=lambda i: (-i[1], i[0]))],
    )


def pair_tasks(baseline: Sequence[RunFacts], candidate: Sequence[RunFacts]) -> list[TaskResult]:
    """One row per task, with each task's repetitions kept together on each side."""
    sides: dict[str, tuple[list[RunFacts], list[RunFacts]]] = {}
    categories: dict[str, str] = {}
    for index, runs in enumerate((baseline, candidate)):
        for run in runs:
            sides.setdefault(run.task_key, ([], []))[index].append(run)
            categories[run.task_key] = run.category
    results = []
    for task_key, (base_runs, cand_runs) in sides.items():
        base, cand = _task_side(base_runs), _task_side(cand_runs)
        if base.pass_rate is None or cand.pass_rate is None:
            outcome: Outcome = "unscored"
            delta = None
        else:
            delta = cand.pass_rate - base.pass_rate
            outcome = "win" if delta > 0 else "loss" if delta < 0 else "tie"
        results.append(
            TaskResult(
                task_key=task_key,
                category=categories[task_key],
                outcome=outcome,
                baseline=base,
                candidate=cand,
                delta=delta,
            )
        )
    # Worst regressions and biggest wins first.
    return sorted(results, key=lambda t: (_ORDER[t.outcome], -abs(t.delta or 0.0), t.task_key))


def seed_for(*parts: str) -> int:
    """The same inputs always give the same interval, so a stored report can be re-derived."""
    return int.from_bytes(hashlib.sha256("|".join(parts).encode()).digest()[:4], "big")


def bootstrap(pairs: Sequence[tuple[float, float]], seed: int) -> Interval:
    """Resample tasks with replacement. A task's repetitions stay together as its pass rate."""
    if not pairs:
        return Interval(
            baseline=None,
            candidate=None,
            delta=None,
            low=None,
            high=None,
            confidence=CONFIDENCE,
            resamples=0,
            seed=seed,
        )
    n = len(pairs)
    differences = [c - b for b, c in pairs]
    rng = random.Random(seed)  # noqa: S311 - statistics, not security
    means = sorted(sum(rng.choices(differences, k=n)) / n for _ in range(RESAMPLES))
    tail = (1 - CONFIDENCE) / 2
    return Interval(
        baseline=sum(b for b, _ in pairs) / n,
        candidate=sum(c for _, c in pairs) / n,
        delta=sum(differences) / n,
        low=means[int(tail * RESAMPLES)],
        high=means[min(RESAMPLES - 1, int((1 - tail) * RESAMPLES))],
        confidence=CONFIDENCE,
        resamples=RESAMPLES,
        seed=seed,
    )


def compare(
    baseline: Side,
    candidate: Side,
    baseline_runs: Sequence[RunFacts],
    candidate_runs: Sequence[RunFacts],
) -> ComparisonResult:
    check_compatible(baseline, candidate)
    tasks = pair_tasks(baseline_runs, candidate_runs)
    paired = [t for t in tasks if t.outcome != "unscored"]
    success = bootstrap(
        [(t.baseline.pass_rate or 0.0, t.candidate.pass_rate or 0.0) for t in paired],
        seed_for(
            baseline.experiment_id,
            baseline.agent_config_id,
            candidate.experiment_id,
            candidate.agent_config_id,
        ),
    )
    base_card = scorecard(baseline.agent_config_id, baseline_runs)
    cand_card = scorecard(candidate.agent_config_id, candidate_runs)
    notes = []
    if len(paired) < MIN_TASKS:
        notes.append(
            f"Only {len(paired)} paired tasks. With fewer than {MIN_TASKS}, "
            "the difference may be noise."
        )
    if success.low is not None and success.high is not None and success.low <= 0 <= success.high:
        notes.append("The interval includes zero, so this difference may be chance.")
    for card, name in ((base_card, "baseline"), (cand_card, "candidate")):
        if len(card.resolved_models) > 1:
            notes.append(
                f"The {name} ran on more than one model version: "
                f"{', '.join(card.resolved_models)}. A model alias may have moved mid-run."
            )
    if (
        base_card.resolved_models
        and cand_card.resolved_models
        and base_card.resolved_models != cand_card.resolved_models
    ):
        notes.append(
            f"The sides ran on different model versions: baseline "
            f"{', '.join(base_card.resolved_models)}, candidate "
            f"{', '.join(cand_card.resolved_models)}."
        )
    unscored = len(tasks) - len(paired)
    if unscored:
        notes.append(f"{unscored} tasks had no scorable run on one side and are not paired.")
    return ComparisonResult(
        baseline=SideSummary(
            experiment_id=baseline.experiment_id,
            agent_config_id=baseline.agent_config_id,
            config_name=baseline.config_name,
            scorecard=base_card,
        ),
        candidate=SideSummary(
            experiment_id=candidate.experiment_id,
            agent_config_id=candidate.agent_config_id,
            config_name=candidate.config_name,
            scorecard=cand_card,
        ),
        paired_tasks=len(paired),
        wins=sum(1 for t in tasks if t.outcome == "win"),
        losses=sum(1 for t in tasks if t.outcome == "loss"),
        ties=sum(1 for t in tasks if t.outcome == "tie"),
        unscored=unscored,
        success=success,
        may_be_noise=len(paired) < MIN_TASKS
        or success.low is None
        or (success.high is not None and success.low <= 0 <= success.high),
        notes=notes,
        tasks=tasks,
    )
