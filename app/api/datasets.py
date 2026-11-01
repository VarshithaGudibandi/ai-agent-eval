from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.dependencies import Db, Member, Viewer, scope_for
from app.db.audit import record_audit
from app.db.content import TaskDefinition, create_dataset_version, list_datasets
from app.environments.registry import EnvironmentRegistry

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])


class ImportRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tasks: list[TaskDefinition] = Field(min_length=1, max_length=2000)


class VersionOut(BaseModel):
    id: str
    version: int
    content_hash: str
    task_count: int


class DatasetOut(BaseModel):
    id: str
    name: str
    versions: list[VersionOut]


class ImportOut(BaseModel):
    dataset_id: str
    dataset_version_id: str
    version: int
    content_hash: str
    task_count: int


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_dataset(
    body: ImportRequest, request: Request, principal: Member, session: Db
) -> ImportOut:
    """Validate tasks and store them as the next immutable version of the dataset."""
    registry: EnvironmentRegistry = request.app.state.environments
    known = set(registry.keys())
    unknown = sorted({t.environment for t in body.tasks} - known)
    keys = [t.task_key for t in body.tasks]
    if unknown or len(keys) != len(set(keys)):
        problem = f"unknown environments: {unknown}" if unknown else "task keys must be unique"
        raise HTTPException(
            status_code=422, detail={"error": "dataset_invalid", "message": problem}
        )
    scope = scope_for(session, principal)
    version = await create_dataset_version(scope, body.name, body.tasks)
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "dataset.imported",
        "dataset_version",
        version.id,
        {"name": body.name, "task_count": len(body.tasks)},
    )
    return ImportOut(
        dataset_id=version.dataset_id,
        dataset_version_id=version.id,
        version=version.version,
        content_hash=version.content_hash,
        task_count=len(body.tasks),
    )


@router.get("")
async def datasets(principal: Viewer, session: Db) -> list[DatasetOut]:
    """Datasets in this workspace with every version."""
    return [
        DatasetOut(
            id=d.id,
            name=d.name,
            versions=[
                VersionOut(id=v.id, version=v.version, content_hash=v.content_hash, task_count=n)
                for v, n in versions
            ],
        )
        for d, versions in await list_datasets(scope_for(session, principal))
    ]
