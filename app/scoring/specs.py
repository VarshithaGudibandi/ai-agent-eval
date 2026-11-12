from app.db import models
from app.db.eval_specs import NewScorer, ensure_spec, save_spec, scorers_of
from app.db.reviews import LabelledAnswer, labelled_answers
from app.db.scope import WorkspaceScope
from app.db.scores import scoring_input
from app.scoring.calibration import question_hash
from app.scoring.checks import CHECKS
from app.scoring.definitions import DEFAULT_SCORERS, AppliesWhen, ScorerDefinition
from app.scoring.engine import Scorer
from app.scoring.judge_prompts import Transcript
from app.scoring.judges import JUDGES, Example, questions_of
from app.scoring.run_view import RunView

EXCERPT_CHARS = 1500


def _new(definition: ScorerDefinition) -> NewScorer:
    check = CHECKS.get(definition.check)
    return NewScorer(
        key=definition.key,
        check=definition.check,
        # A judge grades the whole run.
        kind=check.kind if check else models.ScorerKind.JUDGE,
        scope=check.scope if check else models.ScorerScope.RUN,
        severity=definition.level,
        threshold=definition.pass_mark,
        params=definition.params,
        applies_when=definition.applies_when.model_dump(mode="json"),
        content_hash=definition.content_hash(),
    )


def definition_of(row: models.Scorer) -> ScorerDefinition:
    return ScorerDefinition(
        key=row.key,
        check=row.check,
        params=row.params,
        severity=row.severity,
        threshold=row.threshold,
        applies_when=AppliesWhen.model_validate(row.applies_when),
    )


async def save_definitions(
    scope: WorkspaceScope, agent_id: str, definitions: list[ScorerDefinition]
) -> tuple[models.EvalSpec, bool]:
    filled = [await with_examples(scope, agent_id, d) for d in definitions]
    return await save_spec(scope, agent_id, [_new(d) for d in filled])


async def ensure_default_spec(scope: WorkspaceScope, agent_id: str) -> None:
    """A new agent is graded by the built-in preset until its team saves a spec."""
    await ensure_spec(scope, agent_id, [_new(d) for d in DEFAULT_SCORERS])


async def scorers_for(scope: WorkspaceScope, spec_id: str) -> list[Scorer]:
    return [Scorer(row.id, definition_of(row)) for row in await scorers_of(scope, spec_id)]


def matching_labels(
    labelled: list[LabelledAnswer], key: str, question_id: str, text: str
) -> list[LabelledAnswer]:
    wanted = (key, question_id, question_hash(text))
    return [a for a in labelled if (a.scorer_key, a.question_id, a.question_hash) == wanted]


async def with_examples(
    scope: WorkspaceScope, agent_id: str, definition: ScorerDefinition
) -> ScorerDefinition:
    """A judge with worked examples from people's labels: per question, the newest run
    labelled yes and the newest labelled no. Taken when the spec is saved, so a judge
    version always asks the same thing."""
    if not definition.is_judge:
        return definition
    params = JUDGES[definition.check].model_validate(definition.params)
    labelled = await labelled_answers(scope, agent_id)
    examples: list[Example] = []
    for question in questions_of(params):
        matching = matching_labels(labelled, definition.key, question.id, question.text)
        for answer in ("yes", "no"):
            newest = next((a for a in matching if a.human == answer), None)
            if newest is None:
                continue
            data = await scoring_input(scope, newest.run_id)
            view = RunView.build(data.spans, data.run.tags, data.config)
            excerpt = Transcript.of(view).render()[:EXCERPT_CHARS]
            examples.append(Example(question_id=question.id, answer=answer, excerpt=excerpt))
    filled = {**definition.params, "examples": [e.model_dump() for e in examples]}
    return ScorerDefinition.model_validate({**definition.model_dump(), "params": filled})
