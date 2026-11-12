from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from app.api.dependencies import Db, Member, scope_for
from app.db.feedback import save_feedback
from app.ingest.redact import redact

router = APIRouter(prefix="/api/v1/feedback", tags=["feedback"])

ID_PATTERN = r"^[A-Za-z0-9._:-]+$"


class FeedbackRequest(BaseModel):
    feedback_id: str = Field(min_length=1, max_length=200, pattern=ID_PATTERN)
    trace_id: str = Field(min_length=1, max_length=200)
    score: float = Field(ge=0, le=1)
    comment: str | None = Field(default=None, max_length=5000)


class FeedbackOut(BaseModel):
    feedback_id: str
    created: bool


@router.post("", status_code=status.HTTP_201_CREATED)
async def post_feedback(
    body: FeedbackRequest, response: Response, principal: Member, session: Db
) -> FeedbackOut:
    """Record a user's rating of a trace: 1 is thumbs up, 0 is thumbs down.

    Sending the same feedback_id again stores nothing new and answers 200.
    """
    comment = None if body.comment is None else redact(body.comment)
    created = await save_feedback(
        scope_for(session, principal), body.feedback_id, body.trace_id, body.score, comment
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return FeedbackOut(feedback_id=body.feedback_id, created=created)
