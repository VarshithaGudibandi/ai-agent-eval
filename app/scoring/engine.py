import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.scoring.checks import CHECKS
from app.scoring.definitions import AppliesWhen, ScorerDefinition
from app.scoring.run_view import RunView

logger = logging.getLogger(__name__)

VerdictStatus = Literal["passed", "failed", "incomplete", "not_scored"]


@dataclass(frozen=True)
class Scorer:
    """A stored scorer version: its id and what it checks."""

    id: str
    definition: ScorerDefinition


@dataclass(frozen=True)
class ScoreResult:
    scorer_id: str
    key: str
    span_id: str | None
    value: float | None
    passed: bool | None
    reason: str
    evidence: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class Verdict:
    status: VerdictStatus
    # The average of the soft scores. None when no soft scorer applied.
    quality: float | None
    failed: tuple[str, ...]
    errored: tuple[str, ...]


def applies(when: AppliesWhen, run: RunView) -> bool:
    if any(run.tags.get(name) != value for name, value in when.tags.items()):
        return False
    if when.tools_used and not {s.name for s in run.tool_calls} & set(when.tools_used):
        return False
    return when.has_expected is None or when.has_expected == run.has_expected


def score(run: RunView, scorers: Sequence[Scorer]) -> list[ScoreResult]:
    """Grade a run with every deterministic scorer that applies to it.

    A scorer that does not apply leaves no result at all, so it counts in no denominator.
    A scorer that crashes leaves an error, never a pass or a fail.
    """
    results: list[ScoreResult] = []
    for scorer in scorers:
        definition = scorer.definition
        # Judges call a model, so they run in their own job; see app.scoring.judge_job.
        if definition.is_judge or not applies(definition.applies_when, run):
            continue
        check = CHECKS[definition.check]
        try:
            outcomes = check.run(run, check.params.model_validate(definition.params))
        except Exception as exc:
            logger.warning(
                "a scorer failed", extra={"scorer": definition.key, "error": type(exc).__name__}
            )
            results.append(
                ScoreResult(
                    scorer.id,
                    definition.key,
                    None,
                    None,
                    None,
                    "",
                    (),
                    f"{type(exc).__name__}: {exc}"[:500],
                )
            )
            continue
        for outcome in outcomes:
            value = min(max(outcome.value, 0.0), 1.0)
            results.append(
                ScoreResult(
                    scorer.id,
                    definition.key,
                    outcome.span_id,
                    value,
                    value >= definition.pass_mark,
                    outcome.reason[:1000],
                    outcome.evidence,
                )
            )
    return results


def verdict(scorers: Sequence[Scorer], results: Sequence[ScoreResult]) -> Verdict:
    """Pass when every hard scorer passes and every soft one meets its threshold.

    A scorer graded per step passes only if every step passed; its value is their average.
    """
    by_scorer = {s.id: s.definition for s in scorers}
    failed: list[str] = []
    errored: list[str] = []
    soft_values: list[float] = []
    graded: dict[str, list[ScoreResult]] = {}
    for result in results:
        graded.setdefault(result.scorer_id, []).append(result)
    for scorer_id, scores in graded.items():
        definition = by_scorer[scorer_id]
        if any(r.error is not None for r in scores):
            errored.append(definition.key)
            continue
        if not all(r.passed for r in scores):
            failed.append(definition.key)
        if definition.level == "soft":
            soft_values.append(sum(r.value or 0.0 for r in scores) / len(scores))
    quality = sum(soft_values) / len(soft_values) if soft_values else None
    status: VerdictStatus
    if failed:
        status = "failed"
    elif errored:
        status = "incomplete"
    elif not graded:
        status = "not_scored"
    else:
        status = "passed"
    return Verdict(status, quality, tuple(sorted(failed)), tuple(sorted(errored)))
