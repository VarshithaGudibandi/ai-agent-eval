from app.db import models
from app.db.eval_specs import NewScorer, ensure_spec, save_spec, scorers_of
from app.db.scope import WorkspaceScope
from app.scoring.checks import CHECKS
from app.scoring.definitions import DEFAULT_SCORERS, AppliesWhen, ScorerDefinition
from app.scoring.engine import Scorer


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
    return await save_spec(scope, agent_id, [_new(d) for d in definitions])


async def ensure_default_spec(scope: WorkspaceScope, agent_id: str) -> None:
    """A new agent is graded by the built-in preset until its team saves a spec."""
    await ensure_spec(scope, agent_id, [_new(d) for d in DEFAULT_SCORERS])


async def scorers_for(scope: WorkspaceScope, spec_id: str) -> list[Scorer]:
    return [Scorer(row.id, definition_of(row)) for row in await scorers_of(scope, spec_id)]
