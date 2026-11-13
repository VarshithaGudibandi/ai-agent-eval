from app.db import models
from app.db.scope import WorkspaceScope


async def reports_of(
    scope: WorkspaceScope,
    comparison_id: str | None,
    candidate_experiment_id: str | None,
    limit: int,
) -> list[models.RegressionReport]:
    """Reports newest first, for one comparison or for comparisons whose candidate is one
    experiment."""
    statement = (
        scope.query(models.RegressionReport)
        .join(models.Comparison, models.Comparison.id == models.RegressionReport.comparison_id)
        .where(models.Comparison.workspace_id == scope.workspace_id)
        .order_by(models.RegressionReport.created_at.desc(), models.RegressionReport.id.desc())
        .limit(limit)
    )
    if comparison_id is not None:
        statement = statement.where(models.RegressionReport.comparison_id == comparison_id)
    if candidate_experiment_id is not None:
        statement = statement.where(
            models.Comparison.candidate["experiment_id"].astext == candidate_experiment_id
        )
    return await scope.all(statement)
