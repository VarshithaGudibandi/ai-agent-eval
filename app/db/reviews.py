from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.db import models
from app.db.ids import new_id
from app.db.scope import WorkspaceScope


async def add_review_item(scope: WorkspaceScope, run_id: str, reason: str) -> bool:
    """Queue a run for review. False if it is already in the queue or was reviewed."""
    added = await scope.session.scalar(
        insert(models.ReviewItem)
        .values(
            id=new_id("rev"),
            workspace_id=scope.workspace_id,
            run_id=run_id,
            reason=reason,
            status=models.ReviewStatus.PENDING,
        )
        .on_conflict_do_nothing(index_elements=["run_id"])
        .returning(models.ReviewItem.id)
    )
    return added is not None


async def pending_items(scope: WorkspaceScope, limit: int) -> list[models.ReviewItem]:
    return await scope.all(
        scope.query(models.ReviewItem)
        .where(models.ReviewItem.status == models.ReviewStatus.PENDING)
        .order_by(models.ReviewItem.created_at)
        .limit(limit)
    )


@dataclass(frozen=True)
class NewLabel:
    scorer_id: str
    question_id: str
    question_hash: str
    answer: str


async def save_labels(
    scope: WorkspaceScope,
    item: models.ReviewItem,
    labels: list[NewLabel],
    actor_id: str,
    note: str | None,
) -> None:
    """Store a person's labels. Labelling the same question again replaces the answer."""
    for label in labels:
        await scope.session.execute(
            insert(models.ReviewLabel)
            .values(
                id=new_id("lbl"),
                workspace_id=scope.workspace_id,
                review_item_id=item.id,
                run_id=item.run_id,
                **vars(label),
            )
            .on_conflict_do_update(
                index_elements=["review_item_id", "scorer_id", "question_id"],
                set_={"answer": label.answer, "question_hash": label.question_hash},
            )
        )
    item.status = models.ReviewStatus.LABELED
    item.labeled_by = actor_id
    item.labeled_at = func.now()
    item.note = note


@dataclass(frozen=True)
class LabelledAnswer:
    """A judge's answer and a person's label for the same question on the same run."""

    scorer_key: str
    question_id: str
    question_hash: str
    question_text: str
    judge: str
    human: str
    run_id: str
    quote: str | None


async def labelled_answers(scope: WorkspaceScope, agent_id: str) -> list[LabelledAnswer]:
    """Every labelled question of the agent's judges, matched to the judge's answer on that
    run. Newest labels first."""
    rows = await scope.session.execute(
        select(models.ReviewLabel, models.JudgeAnswer, models.Scorer.key)
        .join(models.Scorer, models.Scorer.id == models.ReviewLabel.scorer_id)
        .join(
            models.JudgeAnswer,
            (models.JudgeAnswer.run_id == models.ReviewLabel.run_id)
            & (models.JudgeAnswer.scorer_id == models.ReviewLabel.scorer_id)
            & (models.JudgeAnswer.question_id == models.ReviewLabel.question_id),
        )
        .where(
            models.ReviewLabel.workspace_id == scope.workspace_id,
            models.Scorer.agent_id == agent_id,
        )
        .order_by(models.ReviewLabel.created_at.desc())
    )
    return [
        LabelledAnswer(
            key,
            label.question_id,
            label.question_hash,
            answer.question_text,
            answer.answer,
            label.answer,
            label.run_id,
            answer.quote,
        )
        for label, answer, key in rows
    ]


@dataclass(frozen=True)
class NewJudgeAnswer:
    scorer_id: str
    question_id: str
    question_text: str
    question_hash: str
    answer: str
    quote: str | None
    evidence: list[str]
    confidence: float | None
    trusted: bool
    model_version: str
    details: dict[str, Any]


async def save_judge_answers(
    scope: WorkspaceScope, run_id: str, answers: list[NewJudgeAnswer]
) -> None:
    """Store a judge's answers. Like scores, the first answers stored for a run are kept,
    so a rerun cannot leave answers that disagree with the score they produced."""
    if not answers:
        return
    await scope.session.execute(
        insert(models.JudgeAnswer)
        .values(
            [
                {"id": new_id("ja"), "workspace_id": scope.workspace_id, "run_id": run_id}
                | vars(answer)
                for answer in answers
            ]
        )
        .on_conflict_do_nothing(index_elements=["run_id", "scorer_id", "question_id"])
    )


async def questions_on(scope: WorkspaceScope, run_id: str) -> list[tuple[models.JudgeAnswer, str]]:
    """The judge questions asked about a run, each with its scorer's key."""
    rows = await scope.session.execute(
        select(models.JudgeAnswer, models.Scorer.key)
        .join(models.Scorer, models.Scorer.id == models.JudgeAnswer.scorer_id)
        .where(
            models.JudgeAnswer.workspace_id == scope.workspace_id,
            models.JudgeAnswer.run_id == run_id,
        )
        .order_by(models.Scorer.key, models.JudgeAnswer.question_id)
    )
    return [(answer, key) for answer, key in rows]
