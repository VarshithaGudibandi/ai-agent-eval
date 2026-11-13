from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.dependencies import Db, Viewer, scope_for
from app.db import models
from app.db.reviews import questions_on
from app.db.scores import repetitions_of, scores_of, verdicts_of
from app.db.traces import spans_of

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


class SpanOut(BaseModel):
    span_id: str
    parent_span_id: str | None
    kind: str
    name: str
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    input: Any
    output: Any
    error: str | None
    attributes: dict[str, Any]


class ScoreOut(BaseModel):
    scorer_key: str
    scorer_version: int
    check: str
    severity: str
    span_id: str | None
    value: float | None
    passed: bool | None
    reason: str
    evidence: list[str]
    error: str | None


class JudgeAnswerOut(BaseModel):
    scorer_key: str
    question_id: str
    question_text: str
    answer: models.JudgeAnswerValue
    quote: str | None
    evidence: list[str]
    confidence: float | None
    # Whether the question was trusted, so counted, when the judge answered.
    trusted: bool
    model_version: str
    claims: list[dict[str, Any]]


class VerdictOut(BaseModel):
    eval_spec_version: int
    status: models.VerdictStatus
    quality: float | None
    failed: list[str]
    errored: list[str]
    scored_at: datetime


class CaseOut(BaseModel):
    key: str
    input: Any
    expected: Any


class RepetitionOut(BaseModel):
    run_id: str
    repetition: int | None
    # From the run's newest verdict; None while it waits to be scored.
    status: models.VerdictStatus | None


class RunOut(BaseModel):
    id: str
    trace_id: str
    agent_version_id: str
    experiment_id: str | None
    case_id: str | None
    repetition: int | None
    tags: dict[str, Any]
    started_at: datetime
    duration_ms: int
    input_tokens: int
    output_tokens: int
    cost_micros: int
    cost_known: bool
    spans: list[SpanOut]
    scores: list[ScoreOut]
    judge_answers: list[JudgeAnswerOut]
    # One per eval spec version the run was graded by, oldest first.
    verdicts: list[VerdictOut]
    # The dataset case the run answered, for experiment runs.
    case: CaseOut | None
    # Every run of the same case in the same experiment, this one included.
    repetitions: list[RepetitionOut]


@router.get("/{run_id}")
async def run(run_id: str, principal: Viewer, session: Db) -> RunOut:
    """A run with its span tree, its scores and its verdicts."""
    scope = scope_for(session, principal)
    row = await scope.require(models.Run, run_id)
    trace = await scope.require(models.Trace, row.trace_row_id)
    case = await scope.get(models.DatasetCase, row.case_id) if row.case_id else None
    repetitions = (
        await repetitions_of(scope, row.experiment_id, row.case_id)
        if row.experiment_id and row.case_id
        else []
    )
    return RunOut(
        id=row.id,
        trace_id=trace.trace_id,
        agent_version_id=row.agent_version_id,
        experiment_id=row.experiment_id,
        case_id=row.case_id,
        repetition=row.repetition,
        tags=row.tags,
        started_at=row.started_at,
        duration_ms=row.duration_ms,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cost_micros=row.cost_micros,
        cost_known=row.cost_known,
        spans=[
            SpanOut(
                span_id=s.span_id,
                parent_span_id=s.parent_span_id,
                kind=s.kind,
                name=s.name,
                started_at=s.started_at,
                ended_at=s.ended_at,
                duration_ms=s.duration_ms,
                input=s.input,
                output=s.output,
                error=s.error,
                attributes=s.attributes,
            )
            for s in await spans_of(scope, run_id)
        ],
        scores=[
            ScoreOut(
                scorer_key=s.scorer.key,
                scorer_version=s.scorer.version,
                check=s.scorer.check,
                severity=s.scorer.severity,
                span_id=s.score.span_id,
                value=s.score.value,
                passed=s.score.passed,
                reason=s.score.reason,
                evidence=s.score.evidence,
                error=s.score.error,
            )
            for s in await scores_of(scope, run_id)
        ],
        judge_answers=[
            JudgeAnswerOut(
                scorer_key=key,
                question_id=a.question_id,
                question_text=a.question_text,
                answer=models.JudgeAnswerValue(a.answer),
                quote=a.quote,
                evidence=a.evidence,
                confidence=a.confidence,
                trusted=a.trusted,
                model_version=a.model_version,
                claims=a.details.get("claims", []),
            )
            for a, key in await questions_on(scope, run_id)
        ],
        verdicts=[
            VerdictOut(
                eval_spec_version=spec.version,
                status=models.VerdictStatus(v.status),
                quality=v.quality,
                failed=v.failed,
                errored=v.errored,
                scored_at=v.created_at,
            )
            for v, spec in await verdicts_of(scope, run_id)
        ],
        case=CaseOut(key=case.case_key, input=case.input, expected=case.expected) if case else None,
        repetitions=[
            RepetitionOut(run_id=r, repetition=n, status=models.VerdictStatus(s) if s else None)
            for r, n, s in repetitions
        ],
    )
