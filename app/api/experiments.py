from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from app.api.dependencies import Db, Member, Viewer, scope_for
from app.db import models
from app.db.audit import record_audit
from app.db.experiments import (
    create_experiment,
    find_by_idempotency_key,
    list_experiments,
    list_runs,
    run_counts,
)
from app.db.scope import WorkspaceScope

router = APIRouter(prefix="/api/v1/experiments", tags=["experiments"])


class NewExperimentRequest(BaseModel):
    dataset_version_id: str
    agent_config_ids: list[str] = Field(min_length=1, max_length=20)
    repetitions: int = Field(default=1, ge=1, le=100)
    mode: models.ExperimentMode


class ExperimentOut(BaseModel):
    id: str
    dataset_version_id: str
    mode: models.ExperimentMode
    status: models.ExperimentStatus
    repetitions: int
    created_at: datetime
    runs_by_evaluation_status: dict[str, int]
    manifest: dict[str, Any]


class RunOut(BaseModel):
    id: str
    agent_config_id: str
    task_version_id: str
    repetition: int
    execution_status: models.ExecutionStatus
    evaluation_status: models.EvaluationStatus

    @classmethod
    def of(cls, row: models.Run) -> "RunOut":
        return cls(
            id=row.id,
            agent_config_id=row.agent_config_id,
            task_version_id=row.task_version_id,
            repetition=row.repetition,
            execution_status=models.ExecutionStatus(row.execution_status),
            evaluation_status=models.EvaluationStatus(row.evaluation_status),
        )


async def _out(scope: WorkspaceScope, rows: list[models.Experiment]) -> list[ExperimentOut]:
    counts = await run_counts(scope, [r.id for r in rows])
    return [
        ExperimentOut(
            id=r.id,
            dataset_version_id=r.dataset_version_id,
            mode=models.ExperimentMode(r.mode),
            status=models.ExperimentStatus(r.status),
            repetitions=r.repetitions,
            created_at=r.created_at,
            runs_by_evaluation_status=counts.get(r.id, {}),
            manifest=r.manifest,
        )
        for r in rows
    ]


def _same_request(existing: models.Experiment, body: NewExperimentRequest) -> bool:
    configs = [c["id"] for c in existing.manifest["agent_configs"]]
    return (
        existing.dataset_version_id == body.dataset_version_id
        and configs == body.agent_config_ids
        and existing.repetitions == body.repetitions
        and existing.mode == body.mode
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def new_experiment(
    body: NewExperimentRequest,
    request: Request,
    response: Response,
    principal: Member,
    session: Db,
    idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
) -> ExperimentOut:
    """Create an experiment with all its runs in one transaction.

    Sending the same Idempotency-Key again returns the first experiment instead of a copy.
    """
    if len(set(body.agent_config_ids)) != len(body.agent_config_ids):
        raise HTTPException(status_code=422, detail={"error": "duplicate_agent_configs"})
    scope = scope_for(session, principal)
    if idempotency_key is not None:
        existing = await find_by_idempotency_key(scope, idempotency_key)
        if existing is not None:
            if not _same_request(existing, body):
                raise HTTPException(status_code=409, detail={"error": "idempotency_key_reused"})
            response.status_code = status.HTTP_200_OK
            return (await _out(scope, [existing]))[0]
    experiment = await create_experiment(
        scope,
        body.dataset_version_id,
        body.agent_config_ids,
        request.app.state.evaluators,
        mode=body.mode,
        repetitions=body.repetitions,
        idempotency_key=idempotency_key,
    )
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "experiment.created",
        "experiment",
        experiment.id,
        {"mode": body.mode},
    )
    await session.refresh(experiment)
    return (await _out(scope, [experiment]))[0]


@router.get("")
async def experiments(principal: Viewer, session: Db) -> list[ExperimentOut]:
    """Experiments in this workspace, newest first."""
    scope = scope_for(session, principal)
    return await _out(scope, await list_experiments(scope))


@router.get("/{experiment_id}")
async def experiment(experiment_id: str, principal: Viewer, session: Db) -> ExperimentOut:
    """One experiment with its manifest and progress."""
    scope = scope_for(session, principal)
    return (await _out(scope, [await scope.require(models.Experiment, experiment_id)]))[0]


@router.get("/{experiment_id}/runs")
async def runs(
    experiment_id: str,
    principal: Viewer,
    session: Db,
    evaluation_status: models.EvaluationStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RunOut]:
    """Runs of an experiment, filtered and paginated."""
    rows = await list_runs(
        scope_for(session, principal),
        experiment_id,
        evaluation_status=evaluation_status,
        limit=limit,
        offset=offset,
    )
    return [RunOut.of(r) for r in rows]
