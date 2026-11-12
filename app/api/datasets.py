from fastapi import APIRouter
from pydantic import BaseModel

from app.api.dependencies import Db, Viewer, scope_for
from app.db.datasets import list_datasets

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])


class VersionOut(BaseModel):
    id: str
    version: int
    content_hash: str


class DatasetOut(BaseModel):
    id: str
    name: str
    versions: list[VersionOut]


@router.get("")
async def datasets(principal: Viewer, session: Db) -> list[DatasetOut]:
    """Datasets in this workspace with every version."""
    return [
        DatasetOut(
            id=d.id,
            name=d.name,
            versions=[
                VersionOut(id=v.id, version=v.version, content_hash=v.content_hash)
                for v in versions
            ],
        )
        for d, versions in await list_datasets(scope_for(session, principal))
    ]
