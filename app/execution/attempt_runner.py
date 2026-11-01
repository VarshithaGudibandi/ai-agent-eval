import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.contract import HARD_STOPS, AgentResult, RunLimits
from app.agents.factory import build_adapter
from app.db import models
from app.db.ids import new_id
from app.environments.contract import AgentTaskInput, TaskVersion
from app.environments.registry import EnvironmentRegistry
from app.environments.state_store import EnvStateStore
from app.evaluators.contract import EvalContext, Evaluator, Score, TraceEvent
from app.execution.trace_recorder import TraceRecorder
from app.gateway.tool_gateway import (
    AttemptContext,
    InProcessToolSession,
    ToolGateway,
    public_schemas,
)

logger = logging.getLogger(__name__)

EXECUTION_STATUS_BY_STOP = {
    "timeout": models.ExecutionStatus.TIMED_OUT,
    "budget": models.ExecutionStatus.BUDGET_EXCEEDED,
}


@dataclass(frozen=True)
class Platform:
    sessionmaker: async_sessionmaker[AsyncSession]
    environments: EnvironmentRegistry
    evaluators: Sequence[Evaluator]
    gateway: ToolGateway = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "gateway", ToolGateway(self.sessionmaker, self.environments))


@dataclass(frozen=True)
class _Loaded:
    attempt_id: str
    workspace_id: str
    run_id: str
    experiment_id: str
    env_id: str
    task: TaskVersion
    adapter_type: str
    adapter_settings: dict[str, Any]
    limits: RunLimits


def overall_status(scores: Sequence[Score], stop_reason: str) -> models.EvaluationStatus:
    if stop_reason in HARD_STOPS or stop_reason == "agent_error":
        return models.EvaluationStatus.FAIL
    if any(s.status == "evaluation_error" for s in scores):
        return models.EvaluationStatus.EVALUATION_ERROR
    applicable = [s for s in scores if s.applicable]
    if not applicable:
        return models.EvaluationStatus.NOT_APPLICABLE
    if any(s.status == "fail" for s in applicable):
        return models.EvaluationStatus.FAIL
    if any(s.status == "partial" for s in applicable):
        return models.EvaluationStatus.PARTIAL
    return models.EvaluationStatus.PASS


async def run_attempt(
    platform: Platform, workspace_id: str, attempt_id: str
) -> models.EvaluationStatus:
    """Execute one attempt end to end: set up, run the agent, snapshot, grade, finalize."""
    started = time.monotonic()
    loaded = await _load_and_mark_running(platform, workspace_id, attempt_id)
    try:
        return await _execute(platform, loaded, started)
    except Exception:
        logger.exception("attempt failed outside the agent", extra={"attempt_id": attempt_id})
        await _mark_infrastructure_error(platform, loaded, started)
        raise


async def _execute(platform: Platform, loaded: _Loaded, started: float) -> models.EvaluationStatus:
    recorder = TraceRecorder(platform.sessionmaker, loaded.workspace_id, loaded.attempt_id)
    async with platform.sessionmaker.begin() as session:
        env = platform.environments.create(
            loaded.task.environment, EnvStateStore(session, loaded.workspace_id)
        )
        await env.setup(loaded.env_id, loaded.task.fixture)
        await _save_snapshot(
            session, loaded, models.SnapshotKind.INITIAL, await env.snapshot(loaded.env_id)
        )
        agent_input: AgentTaskInput = env.agent_view(loaded.task)
        schemas = public_schemas(env.tool_schemas(), None)

    await recorder.record(
        "attempt_started",
        {
            "agent_input": agent_input.model_dump(),
            "tools": [s.name for s in schemas],
            "limits": loaded.limits.model_dump(),
        },
    )
    ctx = AttemptContext(
        attempt_id=loaded.attempt_id,
        workspace_id=loaded.workspace_id,
        env_id=loaded.env_id,
        environment=loaded.task.environment,
        task_context=agent_input.context,
    )
    tools = InProcessToolSession(platform.gateway, ctx, recorder, schemas)
    result = await _run_agent(loaded, agent_input, tools, recorder)
    await recorder.record(
        "agent_finished",
        {"final_response": result.final_response, "stop_reason": result.stop_reason},
    )

    async with platform.sessionmaker.begin() as session:
        env = platform.environments.create(
            loaded.task.environment, EnvStateStore(session, loaded.workspace_id)
        )
        final_state = await env.snapshot(loaded.env_id)
        await _save_snapshot(session, loaded, models.SnapshotKind.FINAL, final_state)
        await env.teardown(loaded.env_id)

    scores = await _evaluate(platform, loaded, final_state, result.final_response)
    status = overall_status(scores, result.stop_reason)
    await _finalize(platform, loaded, result, scores, status, started)
    return status


async def _run_agent(
    loaded: _Loaded,
    agent_input: AgentTaskInput,
    tools: InProcessToolSession,
    recorder: TraceRecorder,
) -> AgentResult:
    adapter = build_adapter(loaded.adapter_type, loaded.adapter_settings)
    try:
        async with asyncio.timeout(loaded.limits.timeout_seconds):
            return await adapter.run(agent_input, tools, recorder, loaded.limits)
    except TimeoutError:
        return AgentResult("", "timeout")
    except Exception as exc:
        # The agent's own failure is a result to grade, not a platform error.
        logger.warning("agent raised", exc_info=True, extra={"attempt_id": loaded.attempt_id})
        return AgentResult("", "agent_error", {"error": type(exc).__name__})


async def _evaluate(
    platform: Platform, loaded: _Loaded, final_state: dict[str, Any], final_response: str
) -> list[Score]:
    async with platform.sessionmaker() as session:
        rows = await session.scalars(
            select(models.TraceEvent)
            .where(
                models.TraceEvent.workspace_id == loaded.workspace_id,
                models.TraceEvent.attempt_id == loaded.attempt_id,
            )
            .order_by(models.TraceEvent.sequence_number)
        )
        trace = [
            TraceEvent(
                id=r.id,
                sequence_number=r.sequence_number,
                event_type=r.event_type,
                payload=r.payload,
            )
            for r in rows
        ]
    ctx = EvalContext(
        task=loaded.task, final_snapshot=final_state, trace=trace, final_response=final_response
    )
    scores: list[Score] = []
    for evaluator in platform.evaluators:
        if not evaluator.applies_to(loaded.task):
            continue
        try:
            scores.extend(await evaluator.evaluate(ctx))
        except Exception as exc:
            logger.exception("evaluator crashed", extra={"evaluator": evaluator.name})
            scores.append(
                Score(
                    evaluator=evaluator.name,
                    version=evaluator.version,
                    layer=evaluator.layer,
                    dimension="evaluation",
                    applicable=True,
                    status="evaluation_error",
                    score=None,
                    reason_code="EVALUATOR_CRASHED",
                    evidence={"error": type(exc).__name__},
                )
            )
    return scores


async def _load_and_mark_running(platform: Platform, workspace_id: str, attempt_id: str) -> _Loaded:
    async with platform.sessionmaker.begin() as session:
        row = (
            await session.execute(
                select(models.Attempt, models.Run, models.TaskVersion, models.AgentConfig)
                .join(models.Run, models.Run.id == models.Attempt.run_id)
                .join(models.TaskVersion, models.TaskVersion.id == models.Run.task_version_id)
                .join(models.AgentConfig, models.AgentConfig.id == models.Run.agent_config_id)
                .where(models.Attempt.workspace_id == workspace_id, models.Attempt.id == attempt_id)
            )
        ).one_or_none()
        if row is None:
            raise LookupError(attempt_id)
        attempt, run, task, config = row
        if attempt.execution_status != models.ExecutionStatus.QUEUED:
            raise RuntimeError(f"attempt {attempt_id} is {attempt.execution_status}, not queued")
        now = datetime.now(UTC)
        attempt.execution_status = models.ExecutionStatus.RUNNING
        attempt.started_at = now
        run.execution_status = models.ExecutionStatus.RUNNING
        experiment = await session.get_one(models.Experiment, run.experiment_id)
        if experiment.status == models.ExperimentStatus.QUEUED:
            experiment.status = models.ExperimentStatus.RUNNING
        return _Loaded(
            attempt_id=attempt.id,
            workspace_id=workspace_id,
            run_id=run.id,
            experiment_id=run.experiment_id,
            env_id=attempt.env_id,
            task=TaskVersion(
                id=task.id,
                task_key=task.task_key,
                environment=task.environment,
                scenario_family=task.scenario_family,
                split=task.split,
                agent_input=AgentTaskInput.model_validate(task.agent_input),
                fixture=task.fixture,
                evaluator_spec=task.evaluator_spec,
            ),
            adapter_type=config.adapter_type,
            adapter_settings=config.settings,
            limits=RunLimits.model_validate(config.limits).tighter(
                RunLimits.model_validate(task.limits)
            ),
        )


async def _save_snapshot(
    session: AsyncSession, loaded: _Loaded, kind: models.SnapshotKind, state: dict[str, Any]
) -> None:
    session.add(
        models.EnvSnapshot(
            id=new_id("snap"),
            workspace_id=loaded.workspace_id,
            attempt_id=loaded.attempt_id,
            kind=kind,
            state=state,
        )
    )


async def _finalize(
    platform: Platform,
    loaded: _Loaded,
    result: AgentResult,
    scores: list[Score],
    status: models.EvaluationStatus,
    started: float,
) -> None:
    execution_status = EXECUTION_STATUS_BY_STOP.get(
        result.stop_reason, models.ExecutionStatus.COMPLETED
    )
    async with platform.sessionmaker.begin() as session:
        for score in scores:
            session.add(
                models.Score(
                    id=new_id("score"),
                    workspace_id=loaded.workspace_id,
                    attempt_id=loaded.attempt_id,
                    evaluator_name=score.evaluator,
                    evaluator_version=score.version,
                    layer=score.layer,
                    dimension=score.dimension,
                    applicable=score.applicable,
                    status=score.status,
                    score=score.score,
                    reason_code=score.reason_code,
                    evidence=score.evidence,
                )
            )
        attempt = await session.get_one(models.Attempt, loaded.attempt_id)
        attempt.execution_status = execution_status
        attempt.stop_reason = result.stop_reason
        attempt.final_response = result.final_response
        attempt.finished_at = datetime.now(UTC)
        attempt.duration_ms = round((time.monotonic() - started) * 1000)
        run = await session.get_one(models.Run, loaded.run_id)
        run.execution_status = execution_status
        run.evaluation_status = status
        await session.flush()
        await _complete_experiment_if_done(session, loaded.experiment_id)


async def _mark_infrastructure_error(platform: Platform, loaded: _Loaded, started: float) -> None:
    async with platform.sessionmaker.begin() as session:
        attempt = await session.get_one(models.Attempt, loaded.attempt_id)
        attempt.execution_status = models.ExecutionStatus.INFRASTRUCTURE_ERROR
        attempt.finished_at = datetime.now(UTC)
        attempt.duration_ms = round((time.monotonic() - started) * 1000)
        run = await session.get_one(models.Run, loaded.run_id)
        run.execution_status = models.ExecutionStatus.INFRASTRUCTURE_ERROR


async def _complete_experiment_if_done(session: AsyncSession, experiment_id: str) -> None:
    unfinished = await session.scalar(
        select(models.Run.id)
        .where(
            models.Run.experiment_id == experiment_id,
            models.Run.execution_status.in_(
                [models.ExecutionStatus.QUEUED, models.ExecutionStatus.RUNNING]
            ),
        )
        .limit(1)
    )
    if unfinished is None:
        experiment = await session.get_one(models.Experiment, experiment_id)
        experiment.status = models.ExperimentStatus.COMPLETED
