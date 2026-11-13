from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.dependencies import Db, Member, Viewer, scope_for
from app.auth.principal import Principal
from app.db import models
from app.db.audit import record_audit
from app.db.datasets import (
    CaseData,
    add_cases,
    add_run_cases,
    cases_of,
    list_datasets,
    save_version,
)
from app.db.scope import WorkspaceScope
from app.db.scores import last_results
from app.ingest.redact import redact_value

router = APIRouter(prefix="/api/v1", tags=["datasets"])

Tag = str | int | float | bool


class CaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    input: Any
    expected: Any = None
    tags: dict[str, Tag] = Field(default_factory=dict, max_length=50)
    notes: str | None = Field(default=None, max_length=5000)


class NewVersionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    cases: list[CaseIn] = Field(min_length=1, max_length=5000)


class AddCasesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: list[CaseIn] = Field(default_factory=list, max_length=5000)
    # Runs whose inputs become cases with no expected outcome, such as failures worth
    # keeping as tests. A run already in the dataset is skipped.
    run_ids: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def one_source(self) -> "AddCasesRequest":
        if bool(self.cases) == bool(self.run_ids):
            raise ValueError("send cases or run_ids, not both and not neither")
        return self


class VersionOut(BaseModel):
    id: str
    dataset_id: str
    version: int
    content_hash: str
    case_count: int


class SavedVersionOut(VersionOut):
    # False when the cases matched the newest version, which was reused.
    created: bool


class DatasetOut(BaseModel):
    id: str
    name: str
    versions: list[VersionOut]


class LastResultOut(BaseModel):
    run_id: str
    # None while the run waits to be scored.
    status: models.VerdictStatus | None


def _last(found: tuple[str, str | None] | None) -> LastResultOut | None:
    if found is None:
        return None
    run_id, status = found
    return LastResultOut(run_id=run_id, status=models.VerdictStatus(status) if status else None)


class CaseOut(BaseModel):
    id: str
    key: str
    input: Any
    expected: Any
    tags: dict[str, Any]
    notes: str | None
    # The newest run of this case, from any experiment. None if it never ran.
    last_result: LastResultOut | None


class VersionWithCasesOut(VersionOut):
    cases: list[CaseOut]


def _data(cases: list[CaseIn]) -> list[CaseData]:
    # Cases are written by people and copied from traces, so they are masked like traces.
    return [
        CaseData(c.key, redact_value(c.input), redact_value(c.expected), c.tags, c.notes)
        for c in cases
    ]


def _saved(
    scope: WorkspaceScope,
    principal: Principal,
    version: models.DatasetVersion,
    created: bool,
    count: int,
) -> SavedVersionOut:
    if created:
        record_audit(
            scope,
            principal.actor_type,
            principal.actor_id,
            "dataset_version.created",
            "dataset_version",
            version.id,
            {"case_count": count},
        )
    return SavedVersionOut(
        id=version.id,
        dataset_id=version.dataset_id,
        version=version.version,
        content_hash=version.content_hash,
        case_count=count,
        created=created,
    )


@router.post("/datasets", status_code=status.HTTP_201_CREATED)
async def new_version(body: NewVersionRequest, principal: Member, session: Db) -> SavedVersionOut:
    """Store cases as the dataset's next version, creating the dataset on first use.

    Cases identical to the newest version reuse it instead of adding a version.
    """
    scope = scope_for(session, principal)
    version, created = await save_version(scope, body.name, _data(body.cases))
    return _saved(scope, principal, version, created, len(body.cases))


@router.post("/datasets/{dataset_id}/cases", status_code=status.HTTP_201_CREATED)
async def more_cases(
    dataset_id: str, body: AddCasesRequest, principal: Member, session: Db
) -> SavedVersionOut:
    """A new version with these cases added to the newest version's."""
    scope = scope_for(session, principal)
    if body.run_ids:
        version, added = await add_run_cases(scope, dataset_id, body.run_ids)
        created = added > 0
    else:
        version, created = await add_cases(scope, dataset_id, _data(body.cases))
    count = len(await cases_of(scope, version.id))
    return _saved(scope, principal, version, created, count)


@router.get("/datasets")
async def datasets(principal: Viewer, session: Db) -> list[DatasetOut]:
    """Datasets in this workspace with every version."""
    return [
        DatasetOut(
            id=d.id,
            name=d.name,
            versions=[
                VersionOut(
                    id=v.id,
                    dataset_id=v.dataset_id,
                    version=v.version,
                    content_hash=v.content_hash,
                    case_count=n,
                )
                for v, n in versions
            ],
        )
        for d, versions in await list_datasets(scope_for(session, principal))
    ]


@router.get("/dataset-versions/{version_id}")
async def dataset_version(version_id: str, principal: Viewer, session: Db) -> VersionWithCasesOut:
    """One dataset version with its cases, in order."""
    scope = scope_for(session, principal)
    version = await scope.require(models.DatasetVersion, version_id)
    cases = await cases_of(scope, version.id)
    results = await last_results(scope, [c.id for c in cases])
    return VersionWithCasesOut(
        id=version.id,
        dataset_id=version.dataset_id,
        version=version.version,
        content_hash=version.content_hash,
        case_count=len(cases),
        cases=[
            CaseOut(
                id=c.id,
                key=c.case_key,
                input=c.input,
                expected=c.expected,
                tags=c.tags,
                notes=c.notes,
                last_result=_last(results.get(c.id)),
            )
            for c in cases
        ],
    )
