from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.dependencies import Db, Viewer, scope_for
from app.api.experiments import RunOut
from app.db import models
from app.db.attempts import (
    attempts_for_run,
    scores_for_attempt,
    snapshots_for_attempt,
    trace_events,
)
from app.reports.state_diff import RecordChange, state_diff

router = APIRouter(prefix="/api/v1", tags=["runs"])


class ScoreOut(BaseModel):
    evaluator: str
    version: str
    layer: str
    dimension: str
    applicable: bool
    status: str
    score: float | None
    reason_code: str | None
    evidence: dict[str, Any]


class AttemptOut(BaseModel):
    id: str
    attempt_number: int
    execution_status: models.ExecutionStatus
    stop_reason: str | None
    final_response: str | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None


class TaskOut(BaseModel):
    task_key: str
    category: str
    environment: str
    split: str
    # Exactly what the agent was given.
    agent_input: dict[str, Any]


class GroundTruthOut(BaseModel):
    """What the agent never saw: the hidden world, how it is graded and the faults injected."""

    fixture: dict[str, Any]
    evaluator_spec: dict[str, Any]
    faults: list[dict[str, Any]]
    expected_outcome: str | None
    rationale: str | None


class RunDetailOut(RunOut):
    task: TaskOut
    ground_truth: GroundTruthOut
    attempts: list[AttemptOut]
    scores: list[ScoreOut]
    # What the latest attempt changed in its environment.
    state_diff: list[RecordChange]


class EventOut(BaseModel):
    id: str
    sequence_number: int
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime
    elapsed_ms: int


@router.get("/runs/{run_id}")
async def run(run_id: str, principal: Viewer, session: Db) -> RunDetailOut:
    """A run with its task, hidden ground truth, attempts, latest scores and state changes."""
    scope = scope_for(session, principal)
    row = await scope.require(models.Run, run_id)
    task = await scope.require(models.TaskVersion, row.task_version_id)
    attempts = await attempts_for_run(scope, row.id)
    scores = await scores_for_attempt(scope, attempts[-1].id) if attempts else []
    snapshots = await snapshots_for_attempt(scope, attempts[-1].id) if attempts else {}
    final = snapshots.get(models.SnapshotKind.FINAL)
    return RunDetailOut(
        **RunOut.of(row, task).model_dump(),
        task=TaskOut(
            task_key=task.task_key,
            category=task.category,
            environment=task.environment,
            split=task.split,
            agent_input=task.agent_input,
        ),
        ground_truth=GroundTruthOut(
            fixture=task.fixture,
            evaluator_spec=task.evaluator_spec,
            faults=task.faults,
            expected_outcome=task.review.get("expected_outcome"),
            rationale=task.review.get("rationale"),
        ),
        state_diff=state_diff(snapshots.get(models.SnapshotKind.INITIAL, {}), final)
        if final is not None
        else [],
        attempts=[
            AttemptOut(
                id=a.id,
                attempt_number=a.attempt_number,
                execution_status=models.ExecutionStatus(a.execution_status),
                stop_reason=a.stop_reason,
                final_response=a.final_response,
                started_at=a.started_at,
                finished_at=a.finished_at,
                duration_ms=a.duration_ms,
            )
            for a in attempts
        ],
        scores=[
            ScoreOut(
                evaluator=s.evaluator_name,
                version=s.evaluator_version,
                layer=s.layer,
                dimension=s.dimension,
                applicable=s.applicable,
                status=s.status,
                score=s.score,
                reason_code=s.reason_code,
                evidence=s.evidence,
            )
            for s in scores
        ],
    )


@router.get("/attempts/{attempt_id}/events")
async def events(attempt_id: str, principal: Viewer, session: Db) -> list[EventOut]:
    """The ordered trace of one attempt."""
    scope = scope_for(session, principal)
    attempt = await scope.require(models.Attempt, attempt_id)
    return [
        EventOut(
            id=e.id,
            sequence_number=e.sequence_number,
            event_type=e.event_type,
            payload=e.payload,
            occurred_at=e.occurred_at,
            elapsed_ms=e.elapsed_ms,
        )
        for e in await trace_events(scope, attempt.id)
    ]
