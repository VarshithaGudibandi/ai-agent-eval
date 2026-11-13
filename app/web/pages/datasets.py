from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.api import datasets
from app.api.dependencies import Db
from app.db import models
from app.db.scope import NotFoundError
from app.web.rendering import Person, render

router = APIRouter(include_in_schema=False)


@router.get("/datasets")
async def dataset_list(request: Request, principal: Person, session: Db) -> Response:
    return render(
        request,
        "datasets.html",
        principal=principal,
        datasets=await datasets.datasets(principal, session),
    )


@router.get("/dataset-versions/{version_id}")
async def dataset_page(
    request: Request, version_id: str, principal: Person, session: Db
) -> Response:
    version = await datasets.dataset_version(version_id, principal, session)
    dataset = next(
        (d for d in await datasets.datasets(principal, session) if d.id == version.dataset_id),
        None,
    )
    if dataset is None:
        raise NotFoundError(f"datasets:{version.dataset_id}")
    tags: dict[str, dict[str, int]] = {}
    for case in version.cases:
        for key, value in case.tags.items():
            counts = tags.setdefault(key, {})
            counts[str(value)] = counts.get(str(value), 0) + 1
    return render(
        request,
        "dataset.html",
        principal=principal,
        can_edit=principal.role in (models.Role.MEMBER, models.Role.OWNER),
        dataset=dataset,
        version=version,
        newest=dataset.versions[-1],
        tags=tags,
        ran=sum(c.last_result is not None for c in version.cases),
    )
