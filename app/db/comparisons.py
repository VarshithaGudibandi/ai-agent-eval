from app.db import models
from app.db.scope import WorkspaceScope


async def comparisons_of(
    scope: WorkspaceScope, candidate_experiment_id: str | None, limit: int
) -> list[models.Comparison]:
    """Comparisons newest first: all of them, or those whose candidate is one experiment."""
    statement = (
        scope.query(models.Comparison)
        .order_by(models.Comparison.created_at.desc(), models.Comparison.id.desc())
        .limit(limit)
    )
    if candidate_experiment_id is not None:
        statement = statement.where(
            models.Comparison.candidate["experiment_id"].astext == candidate_experiment_id
        )
    return await scope.all(statement)
