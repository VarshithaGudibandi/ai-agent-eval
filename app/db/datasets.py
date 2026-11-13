import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.db import models
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.db.traces import spans_of


class CaseKeysClashError(ValueError):
    """Two cases in one version share a key, so they could not be told apart."""


@dataclass(frozen=True)
class CaseData:
    key: str
    input: Any
    expected: Any = None
    tags: dict[str, Any] | None = None
    notes: str | None = None


def _content_hash(cases: list[CaseData]) -> str:
    canonical = json.dumps(
        [[c.key, c.input, c.expected, c.tags or {}, c.notes] for c in cases],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _dataset(scope: WorkspaceScope, name: str) -> models.Dataset:
    await scope.session.execute(
        insert(models.Dataset)
        .values(id=new_id("ds"), workspace_id=scope.workspace_id, name=name)
        .on_conflict_do_nothing(index_elements=["workspace_id", "name"])
    )
    dataset = await scope.first(scope.query(models.Dataset).where(models.Dataset.name == name))
    if dataset is None:
        raise LookupError(name)
    return dataset


async def _latest(scope: WorkspaceScope, dataset_id: str) -> models.DatasetVersion | None:
    return await scope.first(
        scope.query(models.DatasetVersion)
        .where(models.DatasetVersion.dataset_id == dataset_id)
        .order_by(models.DatasetVersion.version.desc())
        .with_for_update()
    )


async def save_version(
    scope: WorkspaceScope, name: str, cases: list[CaseData]
) -> tuple[models.DatasetVersion, bool]:
    """Store cases as the dataset's next version. Returns the version and whether it is new.

    Cases identical to the newest version reuse it, so uploading the same file on every run
    does not pile up versions.
    """
    keys = [c.key for c in cases]
    if len(keys) != len(set(keys)):
        raise CaseKeysClashError("case keys must be unique within a dataset version")
    dataset = await _dataset(scope, name)
    latest = await _latest(scope, dataset.id)
    digest = _content_hash(cases)
    if latest is not None and latest.content_hash == digest:
        return latest, False
    version = models.DatasetVersion(
        id=new_id("dsv"),
        workspace_id=scope.workspace_id,
        dataset_id=dataset.id,
        version=(latest.version + 1) if latest else 1,
        content_hash=digest,
    )
    scope.add(version)
    await scope.flush()
    for position, case in enumerate(cases):
        scope.add(
            models.DatasetCase(
                id=new_id("case"),
                workspace_id=scope.workspace_id,
                dataset_version_id=version.id,
                case_key=case.key,
                position=position,
                input=case.input,
                expected=case.expected,
                tags=case.tags or {},
                notes=case.notes,
            )
        )
    await scope.flush()
    return version, True


def as_case_data(row: models.DatasetCase) -> CaseData:
    return CaseData(row.case_key, row.input, row.expected, row.tags, row.notes)


async def cases_of(scope: WorkspaceScope, version_id: str) -> list[models.DatasetCase]:
    version = await scope.require(models.DatasetVersion, version_id)
    return await scope.all(
        scope.query(models.DatasetCase)
        .where(models.DatasetCase.dataset_version_id == version.id)
        .order_by(models.DatasetCase.position)
    )


async def add_cases(
    scope: WorkspaceScope, dataset_id: str, cases: list[CaseData]
) -> tuple[models.DatasetVersion, bool]:
    """A new version with these cases added after the newest version's."""
    dataset = await scope.require(models.Dataset, dataset_id)
    latest = await _latest(scope, dataset.id)
    existing = [as_case_data(c) for c in await cases_of(scope, latest.id)] if latest else []
    return await save_version(scope, dataset.name, existing + cases)


def run_case_key(trace_id: str) -> str:
    """The key a case made from a run gets, so the same run is never added twice."""
    return "run-" + re.sub(r"[^A-Za-z0-9._:-]", "-", trace_id)[:190]


async def add_run_cases(
    scope: WorkspaceScope, dataset_id: str, run_ids: list[str]
) -> tuple[models.DatasetVersion, int]:
    """Add the runs' inputs as cases with no expected outcome, skipping runs already in the
    dataset. Returns the version that holds them and how many cases were added."""
    dataset = await scope.require(models.Dataset, dataset_id)
    latest = await _latest(scope, dataset.id)
    existing = [as_case_data(c) for c in await cases_of(scope, latest.id)] if latest else []
    keys = {c.key for c in existing}
    added = []
    for run_id in dict.fromkeys(run_ids):
        run = await scope.require(models.Run, run_id)
        trace = await scope.require(models.Trace, run.trace_row_id)
        key = run_case_key(trace.trace_id)
        if key in keys:
            continue
        root = next(s for s in await spans_of(scope, run.id) if s.parent_span_id is None)
        added.append(CaseData(key, root.input, None, {"source": "run"}, f"From run {run.id}"))
        keys.add(key)
    if latest is not None and not added:
        return latest, 0
    version, _ = await save_version(scope, dataset.name, existing + added)
    return version, len(added)


async def list_datasets(
    scope: WorkspaceScope,
) -> list[tuple[models.Dataset, list[tuple[models.DatasetVersion, int]]]]:
    """Every dataset in the workspace with its versions and case counts, oldest first."""
    datasets = await scope.all(scope.query(models.Dataset).order_by(models.Dataset.name))
    versions = await scope.all(
        scope.query(models.DatasetVersion).order_by(models.DatasetVersion.version)
    )
    rows = await scope.session.execute(
        select(models.DatasetCase.dataset_version_id, func.count())
        .where(models.DatasetCase.workspace_id == scope.workspace_id)
        .group_by(models.DatasetCase.dataset_version_id)
    )
    counts = {version_id: count for version_id, count in rows}
    return [
        (d, [(v, counts.get(v.id, 0)) for v in versions if v.dataset_id == d.id]) for d in datasets
    ]
