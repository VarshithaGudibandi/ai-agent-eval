from collections.abc import Sequence
from dataclasses import dataclass, field

MAX_GROUPS = 5


@dataclass(frozen=True)
class RunFailures:
    """Which scorers a run failed, why, and for judges which questions."""

    run_id: str
    case_key: str | None
    failed: tuple[str, ...]
    reasons: dict[str, str] = field(default_factory=dict)
    # Judge scorer key -> the questions it answered no.
    failed_questions: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class FailureGroup:
    """Runs that failed the same way: one scorer, or one judge question."""

    key: str
    scorer_key: str
    question_id: str | None
    run_ids: tuple[str, ...]
    baseline_count: int
    reasons: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.run_ids)


def _keys(run: RunFailures) -> list[tuple[str, str | None]]:
    keys: list[tuple[str, str | None]] = []
    for scorer in run.failed:
        questions = run.failed_questions.get(scorer)
        keys += [(scorer, q) for q in questions] if questions else [(scorer, None)]
    return keys


def group_failures(
    baseline: Sequence[RunFailures],
    candidate: Sequence[RunFailures],
    worse_cases: set[str] | None,
) -> list[FailureGroup]:
    """The ways the candidate fails more than the baseline, the biggest increase first.

    When the comparison is paired, only runs of cases that got worse count, so a failure
    both versions share is not blamed on the change.
    """

    def counts(runs: Sequence[RunFailures]) -> dict[tuple[str, str | None], list[RunFailures]]:
        found: dict[tuple[str, str | None], list[RunFailures]] = {}
        for run in runs:
            for key in _keys(run):
                found.setdefault(key, []).append(run)
        return found

    relevant = [r for r in candidate if worse_cases is None or (r.case_key in worse_cases)]
    before, after = counts(baseline), counts(relevant)
    groups = []
    for (scorer, question), runs in after.items():
        base = len(before.get((scorer, question), []))
        if len(runs) <= base and worse_cases is None:
            continue
        reasons = tuple(
            dict.fromkeys(r.reasons.get(scorer, "") for r in runs if r.reasons.get(scorer))
        )
        groups.append(
            FailureGroup(
                key=scorer if question is None else f"{scorer}:{question}",
                scorer_key=scorer,
                question_id=question,
                run_ids=tuple(r.run_id for r in runs),
                baseline_count=base,
                reasons=reasons[:3],
            )
        )
    groups.sort(key=lambda g: (-(g.count - g.baseline_count), g.key))
    return groups[:MAX_GROUPS]
