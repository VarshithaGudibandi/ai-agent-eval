from dataclasses import dataclass

from app.db import models
from app.db.eval_specs import current_spec
from app.db.reviews import NewLabel, labelled_answers, questions_on, save_labels
from app.db.scope import WorkspaceScope
from app.scoring.calibration import Calibration, calibrate
from app.scoring.judges import JUDGES, questions_of
from app.scoring.specs import matching_labels, scorers_for


class UnknownQuestionError(ValueError):
    """A label names a question the judges did not ask about this run."""


@dataclass(frozen=True)
class GivenLabel:
    scorer_key: str
    question_id: str
    answer: models.HumanAnswer


async def label_run(
    scope: WorkspaceScope,
    item: models.ReviewItem,
    labels: list[GivenLabel],
    actor_id: str,
    note: str | None,
) -> None:
    asked = {(key, a.question_id): a for a, key in await questions_on(scope, item.run_id)}
    stored = []
    for label in labels:
        answer = asked.get((label.scorer_key, label.question_id))
        if answer is None:
            raise UnknownQuestionError(
                f"no question {label.scorer_key}:{label.question_id} was asked about this run"
            )
        stored.append(
            NewLabel(answer.scorer_id, answer.question_id, answer.question_hash, label.answer)
        )
    await save_labels(scope, item, stored, actor_id, note)


@dataclass(frozen=True)
class QuestionCalibration:
    scorer_key: str
    question_id: str
    question_text: str
    calibration: Calibration


async def calibration_report(scope: WorkspaceScope, agent_id: str) -> list[QuestionCalibration]:
    """How far each judge question in the agent's current spec agrees with people."""
    spec = await current_spec(scope, agent_id)
    if spec is None:
        return []
    labelled = await labelled_answers(scope, agent_id)
    report = []
    for scorer in await scorers_for(scope, spec.id):
        if not scorer.definition.is_judge:
            continue
        params = JUDGES[scorer.definition.check].model_validate(scorer.definition.params)
        for question in questions_of(params):
            pairs = matching_labels(labelled, scorer.definition.key, question.id, question.text)
            report.append(
                QuestionCalibration(
                    scorer.definition.key,
                    question.id,
                    question.text,
                    calibrate((a.judge, a.human) for a in pairs),
                )
            )
    return report
