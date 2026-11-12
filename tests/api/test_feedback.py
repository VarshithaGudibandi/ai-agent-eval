from collections.abc import Callable

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.db import models
from tests.api.conftest import SignIn, token_caller


async def _stored(app: FastAPI, workspace_id: str) -> list[models.Feedback]:
    async with app.state.sessionmaker() as session:
        return list(
            await session.scalars(
                select(models.Feedback).where(models.Feedback.workspace_id == workspace_id)
            )
        )


async def test_feedback_is_stored_once_and_its_comment_masked(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    secret = "sk-" + "a" * 32
    body = {
        "feedback_id": "fb-1",
        "trace_id": "not-sent-yet",
        "score": 0,
        "comment": f"it printed my key {secret}",
    }
    first = await owner.post("/api/v1/feedback", json=body)
    again = await owner.post("/api/v1/feedback", json={**body, "score": 1})
    assert (first.status_code, first.json()) == (201, {"feedback_id": "fb-1", "created": True})
    assert (again.status_code, again.json()["created"]) == (200, False)
    [row] = await _stored(app, owner.workspace_id)
    # The first send wins; the retry changed nothing.
    assert (row.trace_id, row.score) == ("not-sent-yet", 0)
    assert row.comment is not None and secret not in row.comment and "[redacted]" in row.comment


async def test_scores_outside_zero_to_one_are_refused(new_owner: SignIn) -> None:
    owner = await new_owner()
    for score in (-0.1, 1.5):
        response = await owner.post(
            "/api/v1/feedback", json={"feedback_id": "f", "trace_id": "t", "score": score}
        )
        assert response.status_code == 422


async def test_a_viewer_token_cannot_send_feedback(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    viewer = await token_caller(await new_owner(), clients(), role="viewer")
    response = await viewer.post(
        "/api/v1/feedback", json={"feedback_id": "f", "trace_id": "t", "score": 1}
    )
    assert response.status_code == 403
