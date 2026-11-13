from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Response, status
from pydantic import BaseModel, Field

from app.api.dependencies import Db, Member, Viewer, scope_for
from app.db import models
from app.db.audit import record_audit
from app.db.datasets import cases_of
from app.db.experiments import finish_experiment, runs_received, start_experiment
from app.db.outcomes import baseline_experiment, experiment_outcomes
from app.db.scope import WorkspaceScope
from app.db.scores import experiment_scoring
from app.db.traces import agent_version
from app.ingest.redact import redact_value
from app.metrics.health import Health, health
from app.scoring.specs import ensure_default_spec
from app.traces.normalize import fingerprint

router = APIRouter(prefix="/api/v1/experiments", tags=["experiments"])


class NewExperimentRequest(BaseModel):
    agent: str = Field(min_length=1, max_length=200)
    config: dict[str, Any]
    label: str | None = Field(default=None, min_length=1, max_length=100)
    dataset_version_id: str
    repetitions: int = Field(default=1, ge=1, le=100)
    # Caps the model calls its judges may make. Left out, judging is not capped.
    max_judge_calls: int | None = Field(default=None, ge=0, le=10_000_000)


class CaseOut(BaseModel):
    id: str
    key: str
    input: Any
    expected: Any


class ExperimentOut(BaseModel):
    id: str
    agent_version_id: str
    dataset_version_id: str
    status: models.ExperimentStatus
    repetitions: int
    created_at: datetime
    finished_at: datetime | None
    # Cases times repetitions, against how many runs have arrived so far.
    expected_runs: int
    runs_received: int
    # From each run's newest verdict. Scoring runs on the worker, so it trails behind.
    runs_scored: int
    health: Health
    runs_passed: int
    failures: dict[str, int]
    errors: dict[str, int]


class StartedExperimentOut(ExperimentOut):
    cases: list[CaseOut]


async def experiment_out(
    scope: WorkspaceScope, row: models.Experiment, cases: int
) -> ExperimentOut:
    scoring = await experiment_scoring(scope, row.id)
    outcomes = await experiment_outcomes(scope, row.id)
    return ExperimentOut(
        id=row.id,
        agent_version_id=row.agent_version_id,
        dataset_version_id=row.dataset_version_id,
        status=models.ExperimentStatus(row.status),
        repetitions=row.repetitions,
        created_at=row.created_at,
        finished_at=row.finished_at,
        expected_runs=cases * row.repetitions,
        runs_received=await runs_received(scope, row.id),
        runs_scored=scoring.runs_scored,
        health=health(outcomes.outcomes),
        runs_passed=scoring.runs_passed,
        failures=scoring.failures,
        errors=scoring.errors,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def start(
    body: NewExperimentRequest,
    response: Response,
    principal: Member,
    session: Db,
    idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
) -> StartedExperimentOut:
    """Start an experiment and get the cases to run.

    Sending the same Idempotency-Key again returns the first experiment instead of a copy.
    """
    scope = scope_for(session, principal)
    # Masked exactly as ingest masks it, so the experiment and its traces share a version.
    config = redact_value(body.config)
    version = await agent_version(scope, body.agent, fingerprint(config), config, body.label)
    await ensure_default_spec(scope, version.agent_id)
    experiment, created = await start_experiment(
        scope,
        version,
        body.dataset_version_id,
        body.repetitions,
        idempotency_key,
        body.max_judge_calls,
    )
    if created:
        record_audit(
            scope,
            principal.actor_type,
            principal.actor_id,
            "experiment.started",
            "experiment",
            experiment.id,
        )
    else:
        response.status_code = status.HTTP_200_OK
    await scope.flush()
    await session.refresh(experiment)
    cases = await cases_of(scope, experiment.dataset_version_id)
    summary = await experiment_out(scope, experiment, len(cases))
    return StartedExperimentOut(
        **summary.model_dump(),
        cases=[CaseOut(id=c.id, key=c.case_key, input=c.input, expected=c.expected) for c in cases],
    )


@router.get("/{experiment_id}")
async def experiment(experiment_id: str, principal: Viewer, session: Db) -> ExperimentOut:
    """An experiment and how many of its runs have arrived."""
    scope = scope_for(session, principal)
    row = await scope.require(models.Experiment, experiment_id)
    return await experiment_out(scope, row, len(await cases_of(scope, row.dataset_version_id)))


@router.post("/{experiment_id}/finish")
async def finish(experiment_id: str, principal: Member, session: Db) -> ExperimentOut:
    """Mark that every trace has been sent. Finishing twice changes nothing."""
    scope = scope_for(session, principal)
    row = await finish_experiment(scope, experiment_id)
    await scope.flush()
    await session.refresh(row)
    return await experiment_out(scope, row, len(await cases_of(scope, row.dataset_version_id)))


class BaselineOut(BaseModel):
    # None when there is nothing to compare with yet, such as an agent's first experiment.
    experiment_id: str | None
    agent_version_id: str | None


@router.get("/{experiment_id}/baseline")
async def baseline(experiment_id: str, principal: Viewer, session: Db) -> BaselineOut:
    """What this experiment is compared with: the newest finished experiment on the same
    dataset version of the version marked shipped, or else of any other version."""
    scope = scope_for(session, principal)
    found = await baseline_experiment(scope, await scope.require(models.Experiment, experiment_id))
    return BaselineOut(
        experiment_id=found.id if found else None,
        agent_version_id=found.agent_version_id if found else None,
    )
