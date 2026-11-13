from collections.abc import Sequence

from pydantic import BaseModel

from app.explain.auditor import Audit
from app.explain.diff import ConfigChange
from app.explain.groups import FailureGroup


class ChangeOut(BaseModel):
    field: str
    summary: str
    detail: list[str]


class SpanRef(BaseModel):
    run_id: str
    span_id: str


class ClaimOut(BaseModel):
    text: str
    spans: list[SpanRef]


class GroupOut(BaseModel):
    key: str
    scorer_key: str
    question_id: str | None
    count: int
    baseline_count: int
    reasons: list[str]
    example_runs: list[str]
    cause: str | None
    confidence: str | None
    claims: list[ClaimOut]
    next_step: str | None
    # Why there is no cause, such as no Gemini key or a used-up budget.
    error: str | None


class Report(BaseModel):
    headline: str
    changes: list[ChangeOut]
    groups: list[GroupOut]
    next_step: str | None
    limits: list[str]


LIMITS = [
    "This links a change to the failures that followed it; it is not proof of cause.",
]


def assemble(
    changes: Sequence[ConfigChange],
    groups: Sequence[FailureGroup],
    audits: dict[str, Audit | str],
    paired: bool,
) -> Report:
    """Put the deterministic findings and the auditor's checked answers together."""
    outs = []
    for group in groups:
        audit = audits.get(group.key)
        found = audit if isinstance(audit, Audit) else None
        outs.append(
            GroupOut(
                key=group.key,
                scorer_key=group.scorer_key,
                question_id=group.question_id,
                count=group.count,
                baseline_count=group.baseline_count,
                reasons=list(group.reasons),
                example_runs=list(group.run_ids[:5]),
                cause=found.cause if found else None,
                confidence=found.confidence if found else None,
                claims=[
                    ClaimOut(text=c.text, spans=[SpanRef(run_id=r, span_id=s) for r, s in c.spans])
                    for c in (found.claims if found else ())
                ],
                next_step=(found.next_step or None) if found else None,
                error=audit if isinstance(audit, str) else None,
            )
        )
    limits = list(LIMITS)
    if len({c.field for c in changes}) > 1:
        limits.append(
            "Several parts of the config changed at once, so the report cannot tell which one"
            " caused the failures."
        )
    if not paired:
        limits.append(
            "Live runs are not paired case by case, so the link between change and failure is"
            " weaker."
        )
    if not changes:
        limits.append("The two sides share the same config, so the change is not in the config.")
    top = outs[0] if outs else None
    if top is None:
        headline = "No failure grew between the two sides, so there is nothing to explain."
    else:
        what = top.scorer_key + (f" ({top.question_id})" if top.question_id else "")
        headline = f"{what} now fails in {top.count} runs, against {top.baseline_count} before"
        if top.cause and top.confidence != "low":
            headline += f". Likely cause ({top.confidence} confidence): {top.cause}"
    return Report(
        headline=headline,
        changes=[
            ChangeOut(field=c.field, summary=c.summary, detail=list(c.detail)) for c in changes
        ],
        groups=outs,
        next_step=top.next_step if top else None,
        limits=limits,
    )
