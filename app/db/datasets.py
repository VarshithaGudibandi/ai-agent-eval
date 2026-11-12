from app.db import models
from app.db.scope import WorkspaceScope


async def list_datasets(
    scope: WorkspaceScope,
) -> list[tuple[models.Dataset, list[models.DatasetVersion]]]:
    """Every dataset in the workspace with its versions, oldest version first."""
    datasets = await scope.all(scope.query(models.Dataset).order_by(models.Dataset.name))
    versions = await scope.all(
        scope.query(models.DatasetVersion).order_by(models.DatasetVersion.version)
    )
    return [(d, [v for v in versions if v.dataset_id == d.id]) for d in datasets]
