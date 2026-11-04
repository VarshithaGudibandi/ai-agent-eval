from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from app.api.dependencies import Db, Member, Owner, Viewer, scope_for
from app.db import models
from app.db.audit import record_audit
from app.db.budgets import BudgetLimits, budget_for_experiment, has_headroom, raise_limits
from app.db.experiments import (
    create_experiment,
    find_by_idempotency_key,
    list_experiments,
    list_runs,
    requeue_budget_stopped_runs,
    run_counts,
    transition,
)
from app.db.jobs import CANCELLABLE, cancel_work
from app.db.reports import run_facts
from app.db.scope import WorkspaceScope
from app.execution.launch import launch_prices
from app.reports.metrics import Scorecard, scorecards

router = APIRouter(prefix="/api/v1/experiments", tags=["experiments"])


class NewExperimentRequest(BaseModel):
    dataset_version_id: str
    agent_config_ids: list[str] = Field(min_length=1, max_length=20)
    repetitions: int = Field(default=1, ge=1, le=100)
    mode: models.ExperimentMode
    split: models.Split = models.Split.DEV
    final: bool = False
    budget: BudgetLimits = BudgetLimits()


class BudgetOut(BaseModel):
    currency: str
    money_limit_micros: int | None
    tokens_limit: int | None
    requests_limit: int | None
    money_used_micros: int
    tokens_used: int
    requests_used: int
    warning: bool

    @classmethod
    def of(cls, row: models.Budget) -> "BudgetOut":
        return cls(
            currency=row.currency,
            money_limit_micros=row.money_limit_micros,
            tokens_limit=row.tokens_limit,
            requests_limit=row.requests_limit,
            money_used_micros=row.money_used_micros,
            tokens_used=row.tokens_used,
            requests_used=row.requests_used,
            warning=row.warned_at is not None,
        )


class ExperimentOut(BaseModel):
    id: str
    dataset_version_id: str
    mode: models.ExperimentMode
    split: models.Split
    final: bool
    status: models.ExperimentStatus
    repetitions: int
    created_at: datetime
    runs_by_evaluation_status: dict[str, int]
    budget: BudgetOut
    manifest: dict[str, Any]


class ExperimentDetailOut(ExperimentOut):
    scorecards: list[Scorecard]


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
            split=models.Split(r.split),
            final=r.final,
            status=models.ExperimentStatus(r.status),
            repetitions=r.repetitions,
            created_at=r.created_at,
            runs_by_evaluation_status=counts.get(r.id, {}),
            budget=BudgetOut.of(await budget_for_experiment(scope, r.id)),
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
        and existing.split == body.split
        and existing.final == body.final
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
    prices = await launch_prices(
        scope, body.agent_config_ids, request.app.state.providers, body.budget, body.mode
    )
    experiment = await create_experiment(
        scope,
        body.dataset_version_id,
        body.agent_config_ids,
        request.app.state.evaluators,
        mode=body.mode,
        split=body.split,
        final=body.final,
        repetitions=body.repetitions,
        idempotency_key=idempotency_key,
        budget=body.budget,
        prices=prices,
    )
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "experiment.created",
        "experiment",
        experiment.id,
        {"mode": body.mode, "split": body.split, "final": body.final},
    )
    await session.refresh(experiment)
    return (await _out(scope, [experiment]))[0]


@router.get("")
async def experiments(principal: Viewer, session: Db) -> list[ExperimentOut]:
    """Experiments in this workspace, newest first."""
    scope = scope_for(session, principal)
    return await _out(scope, await list_experiments(scope))


@router.get("/{experiment_id}")
async def experiment(experiment_id: str, principal: Viewer, session: Db) -> ExperimentDetailOut:
    """One experiment with its manifest, progress and a scorecard per agent config."""
    scope = scope_for(session, principal)
    row = await scope.require(models.Experiment, experiment_id)
    configs = [c["id"] for c in row.manifest["agent_configs"]]
    return ExperimentDetailOut(
        **(await _out(scope, [row]))[0].model_dump(),
        scorecards=scorecards(configs, await run_facts(scope, row.id)),
    )


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


PAUSED = {models.ExperimentStatus.PAUSED_BUDGET, models.ExperimentStatus.PAUSED_USER}


async def _one(scope: WorkspaceScope, experiment: models.Experiment) -> ExperimentOut:
    await scope.flush()
    await scope.session.refresh(experiment)
    return (await _out(scope, [experiment]))[0]


@router.post("/{experiment_id}/pause")
async def pause(experiment_id: str, principal: Member, session: Db) -> ExperimentOut:
    """Stop starting new runs. Finished runs are kept as they are."""
    scope = scope_for(session, principal)
    experiment = await transition(
        scope,
        experiment_id,
        {models.ExperimentStatus.QUEUED, models.ExperimentStatus.RUNNING},
        models.ExperimentStatus.PAUSED_USER,
    )
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "experiment.paused",
        "experiment",
        experiment.id,
    )
    return await _one(scope, experiment)


@router.post("/{experiment_id}/resume")
async def resume(experiment_id: str, principal: Member, session: Db) -> ExperimentOut:
    """Queue a paused experiment again. Runs the budget stopped are retried from scratch."""
    scope = scope_for(session, principal)
    budget = await budget_for_experiment(
        scope, (await scope.require(models.Experiment, experiment_id)).id
    )
    if not has_headroom(budget):
        raise HTTPException(
            status_code=409,
            detail={"error": "budget_exhausted", "message": "raise the budget before resuming"},
        )
    experiment = await transition(scope, experiment_id, PAUSED, models.ExperimentStatus.QUEUED)
    requeued = await requeue_budget_stopped_runs(scope, experiment.id)
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "experiment.resumed",
        "experiment",
        experiment.id,
        {"requeued_runs": requeued},
    )
    return await _one(scope, experiment)


@router.post("/{experiment_id}/close")
async def close(experiment_id: str, principal: Member, session: Db) -> ExperimentOut:
    """End a paused experiment as partial. Its report says so and shows coverage."""
    scope = scope_for(session, principal)
    experiment = await transition(
        scope, experiment_id, PAUSED, models.ExperimentStatus.CLOSED_PARTIAL
    )
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "experiment.closed",
        "experiment",
        experiment.id,
    )
    return await _one(scope, experiment)


@router.post("/{experiment_id}/cancel")
async def cancel(experiment_id: str, principal: Member, session: Db) -> ExperimentOut:
    """Stop for good. Queued runs are dropped and running agents lose their lease at once."""
    scope = scope_for(session, principal)
    experiment = await transition(
        scope, experiment_id, CANCELLABLE, models.ExperimentStatus.CANCELLED
    )
    in_flight = await cancel_work(scope, experiment.id)
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "experiment.cancelled",
        "experiment",
        experiment.id,
        {"in_flight_attempts": in_flight},
    )
    return await _one(scope, experiment)


@router.patch("/{experiment_id}/budget")
async def change_budget(
    experiment_id: str, body: BudgetLimits, principal: Owner, session: Db
) -> ExperimentOut:
    """Set new caps. A cap can rise or be removed, but never drop below what is used."""
    scope = scope_for(session, principal)
    experiment = await scope.require(models.Experiment, experiment_id)
    await raise_limits(scope, await budget_for_experiment(scope, experiment.id), body)
    record_audit(
        scope,
        principal.actor_type,
        principal.actor_id,
        "budget.changed",
        "experiment",
        experiment.id,
        body.model_dump(),
    )
    return await _one(scope, experiment)
