from sqlalchemy.dialects.postgresql import insert

from app.db import models
from app.db.ids import new_id
from app.db.scope import WorkspaceScope


async def save_feedback(
    scope: WorkspaceScope, feedback_id: str, trace_id: str, score: float, comment: str | None
) -> bool:
    """Store one rating. Returns False if this workspace already has it."""
    row_id = await scope.session.scalar(
        insert(models.Feedback)
        .values(
            id=new_id("fb"),
            workspace_id=scope.workspace_id,
            feedback_id=feedback_id,
            trace_id=trace_id,
            score=score,
            comment=comment,
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "feedback_id"])
        .returning(models.Feedback.id)
    )
    return row_id is not None
