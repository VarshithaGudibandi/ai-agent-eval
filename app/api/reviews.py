from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field

from app.api.dependencies import Db, Member, Viewer, scope_for
from app.db import models
from app.db.audit import record_audit
from app.db.reviews import add_review_item, pending_items, questions_on
from app.db.traces import spans_of
from app.scoring.review import GivenLabel, calibration_report, label_run

router = APIRouter(prefix="/api/v1", tags=["review"])


class QuestionOut(BaseModel):
    scorer_key: str
    question_id: str
    text: str


class ToolCallOut(BaseModel):
    name: str
    input: Any
    output: Any
    error: str | None


class ReviewItemOut(BaseModel):
    id: str
    run_id: str
    reason: models.ReviewReason
    created_at: datetime
    request: Any
    reply: Any
    tool_calls: list[ToolCallOut]
    # The questions to label. The judge's own answers are left out, so they cannot sway
    # the person labelling.
    questions: list[QuestionOut]


class NewReviewItem(BaseModel):
    run_id: str = Field(min_length=1, max_length=100)


class AddedOut(BaseModel):
    run_id: str
    added: bool


class LabelIn(BaseModel):
    scorer_key: str
    question_id: str
    answer: models.HumanAnswer


class LabelsIn(BaseModel):
    labels: list[LabelIn] = Field(min_length=1, max_length=50)
    note: str | None = Field(default=None, max_length=2000)


class LabelledOut(BaseModel):
    id: str
    status: models.ReviewStatus
    labels: int


class CalibrationOut(BaseModel):
    scorer_key: str
    question_id: str
    question_text: str
    labels: int
    agreement: float | None
    cant_tell_rate: float | None
    trusted: bool


@router.get("/review-queue")
async def review_queue(
    principal: Viewer, session: Db, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[ReviewItemOut]:
    """Runs waiting for a person's labels, oldest first."""
    scope = scope_for(session, principal)
    items = []
    for item in await pending_items(scope, limit):
        spans = await spans_of(scope, item.run_id)
        root = next(s for s in spans if s.parent_span_id is None)
        items.append(
            ReviewItemOut(
                id=item.id,
                run_id=item.run_id,
                reason=models.ReviewReason(item.reason),
                created_at=item.created_at,
                request=root.input,
                reply=root.output,
                tool_calls=[
                    ToolCallOut(name=s.name, input=s.input, output=s.output, error=s.error)
                    for s in spans
                    if s.kind == models.SpanKind.TOOL
                ],
                questions=[
                    QuestionOut(scorer_key=key, question_id=a.question_id, text=a.question_text)
                    for a, key in await questions_on(scope, item.run_id)
                ],
            )
        )
    return items


@router.post("/review-items", status_code=status.HTTP_201_CREATED)
async def add_to_review(
    body: NewReviewItem, response: Response, principal: Member, session: Db
) -> AddedOut:
    """Put a run in the review queue. A run already there answers 200."""
    scope = scope_for(session, principal)
    await scope.require(models.Run, body.run_id)
    added = await add_review_item(scope, body.run_id, models.ReviewReason.MANUAL)
    if not added:
        response.status_code = status.HTTP_200_OK
    return AddedOut(run_id=body.run_id, added=added)


@router.post("/review-items/{item_id}/label")
async def label(item_id: str, body: LabelsIn, principal: Member, session: Db) -> LabelledOut:
    """Record a person's yes or no for the judge questions about a run.

    Labelling a question again replaces the earlier answer.
    """
    scope = scope_for(session, principal)
    item = await scope.require(models.ReviewItem, item_id)
    given = [GivenLabel(x.scorer_key, x.question_id, x.answer) for x in body.labels]
    await label_run(scope, item, given, principal.actor_id, body.note)
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "review.labelled",
        "review_item",
        item.id,
        {"labels": len(given)},
    )
    return LabelledOut(id=item.id, status=models.ReviewStatus.LABELED, labels=len(given))


@router.get("/agents/{agent_id}/calibration")
async def calibration(agent_id: str, principal: Viewer, session: Db) -> list[CalibrationOut]:
    """How often each judge question of the agent agrees with people, and whether it is
    trusted yet: at least 10 labels on answered runs and 80% agreement."""
    scope = scope_for(session, principal)
    await scope.require(models.Agent, agent_id)
    return [
        CalibrationOut(
            scorer_key=q.scorer_key,
            question_id=q.question_id,
            question_text=q.question_text,
            labels=q.calibration.labels,
            agreement=q.calibration.agreement,
            cant_tell_rate=q.calibration.cant_tell_rate,
            trusted=q.calibration.trusted,
        )
        for q in await calibration_report(scope, agent_id)
    ]
