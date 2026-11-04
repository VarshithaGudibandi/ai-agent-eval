import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.contract import HARD_STOPS, AgentResult, RunLimits
from app.agents.factory import build_adapter
from app.agents.http import ExternalAgentUnavailableError
from app.agents.http_guard import GuardPolicy
from app.auth.signing_secrets import SigningSecretRequiredError, signing_secret
from app.budget.reservations import BudgetExhaustedError
from app.db import models
from app.db.attempts import AttemptNotLiveError, load_attempt, require_live, trace_events
from app.db.budgets import budget_for_experiment
from app.db.experiments import complete_if_done
from app.db.ids import new_id
from app.db.queue import finish_job
from app.db.scope import WorkspaceScope
from app.db.tool_calls import count_limit_refusals
from app.environments.contract import AgentTaskInput, FaultSpec, TaskVersion
from app.environments.registry import EnvironmentRegistry
from app.environments.state_store import EnvStateStore
from app.evaluators.contract import EvalContext, Evaluator, Score, TraceEvent
from app.execution.external_link import GatewayLink
from app.execution.launch import ProviderKeyRequiredError
from app.execution.model_access import AttemptModels
from app.execution.trace_recorder import TraceRecorder
from app.gateway.tool_gateway import (
    AttemptContext,
    InProcessToolSession,
    ToolGateway,
    public_schemas,
)
from app.providers.contract import ContentBlocked, ProviderError
from app.providers.key_vault import (
    KeyUnreadableError,
    MasterKeyMissingError,
    key_vault_from_settings,
)
from app.providers.registry import ProviderRegistry, UnknownProviderError, default_providers
from app.settings import Settings

logger = logging.getLogger(__name__)

# The model could not be reached or used at all. That says nothing about the agent, so the
# run is left unscored instead of failed.
_MODEL_UNAVAILABLE = (
    ProviderError,
    ProviderKeyRequiredError,
    KeyUnreadableError,
    MasterKeyMissingError,
    UnknownProviderError,
    ExternalAgentUnavailableError,
    SigningSecretRequiredError,
)

EXECUTION_STATUS_BY_STOP = {"timeout": models.ExecutionStatus.TIMED_OUT}


@dataclass(frozen=True)
class Platform:
    sessionmaker: async_sessionmaker[AsyncSession]
    environments: EnvironmentRegistry
    evaluators: Sequence[Evaluator]
    providers: ProviderRegistry = field(default_factory=default_providers)
    settings: Settings = field(default_factory=Settings)
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
    lease_token: str
    budget_id: str
    task: TaskVersion
    agent_config_id: str
    adapter_type: str
    adapter_settings: dict[str, Any]
    limits: RunLimits
    price: dict[str, Any] | None


class _ModelUnavailableError(Exception):
    pass


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
    platform: Platform, workspace_id: str, attempt_id: str, lease_token: str | None = None
) -> models.EvaluationStatus:
    """Execute one attempt end to end: set up, run the agent, snapshot, grade, finalize.

    Every step that changes state checks the lease in the same transaction. If the lease is
    lost, the next step raises AttemptNotLiveError and nothing more is written: whoever took
    the attempt over owns its outcome.
    """
    started = time.monotonic()
    loaded = await _load_and_mark_running(platform, workspace_id, attempt_id, lease_token)
    try:
        return await _execute(platform, loaded, started)
    except AttemptNotLiveError:
        logger.warning("attempt is no longer live; stopping", extra={"attempt_id": attempt_id})
        raise
    except Exception:
        logger.exception("attempt failed outside the agent", extra={"attempt_id": attempt_id})
        await _mark_infrastructure_error(platform, loaded, started)
        raise


async def _execute(platform: Platform, loaded: _Loaded, started: float) -> models.EvaluationStatus:
    recorder = TraceRecorder(
        platform.sessionmaker, loaded.workspace_id, loaded.attempt_id, loaded.lease_token
    )
    async with platform.sessionmaker.begin() as session:
        await require_live(session, loaded.workspace_id, loaded.attempt_id, loaded.lease_token)
        env = platform.environments.create(
            loaded.task.environment, EnvStateStore(session, loaded.workspace_id)
        )
        await env.setup(loaded.env_id, loaded.task.fixture)
        _save_snapshot(
            WorkspaceScope(session, loaded.workspace_id),
            loaded,
            models.SnapshotKind.INITIAL,
            await env.snapshot(loaded.env_id),
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
        faults=loaded.task.faults,
        max_tool_calls=loaded.limits.max_tool_calls,
    )
    tools = InProcessToolSession(platform.gateway, ctx, recorder, schemas)
    model_unavailable: str | None = None
    try:
        result = await _run_agent(platform, loaded, agent_input, tools, recorder)
    except _ModelUnavailableError as exc:
        model_unavailable = str(exc)
        result = AgentResult("", "agent_error", {"model_unavailable": model_unavailable})
    if model_unavailable is None and await _hit_tool_limit(platform, loaded):
        # The gateway refused a call past the limit. Whatever the agent did next, the run hit
        # a hard stop, exactly as if the agent had stopped itself.
        result = AgentResult(result.final_response, "max_tool_calls", result.metadata)
    await recorder.record(
        "agent_finished",
        {
            "final_response": result.final_response,
            "stop_reason": result.stop_reason,
            "model_unavailable": model_unavailable,
        },
    )

    async with platform.sessionmaker.begin() as session:
        await require_live(session, loaded.workspace_id, loaded.attempt_id, loaded.lease_token)
        env = platform.environments.create(
            loaded.task.environment, EnvStateStore(session, loaded.workspace_id)
        )
        final_state = await env.snapshot(loaded.env_id)
        _save_snapshot(
            WorkspaceScope(session, loaded.workspace_id),
            loaded,
            models.SnapshotKind.FINAL,
            final_state,
        )
        await env.teardown(loaded.env_id)

    if model_unavailable is not None or result.stop_reason == "budget":
        # Neither is the agent's doing, so nothing is graded. A budget stop is retried when
        # the experiment resumes.
        execution = (
            models.ExecutionStatus.INFRASTRUCTURE_ERROR
            if model_unavailable is not None
            else models.ExecutionStatus.BUDGET_EXCEEDED
        )
        await _finalize(
            platform, loaded, result, [], models.EvaluationStatus.PENDING, execution, started
        )
        return models.EvaluationStatus.PENDING
    scores = await _evaluate(platform, loaded, final_state, result.final_response)
    status = overall_status(scores, result.stop_reason)
    execution = EXECUTION_STATUS_BY_STOP.get(result.stop_reason, models.ExecutionStatus.COMPLETED)
    await _finalize(platform, loaded, result, scores, status, execution, started)
    return status


async def _run_agent(
    platform: Platform,
    loaded: _Loaded,
    agent_input: AgentTaskInput,
    tools: InProcessToolSession,
    recorder: TraceRecorder,
) -> AgentResult:
    models_for_attempt = AttemptModels(
        sessionmaker=platform.sessionmaker,
        providers=platform.providers,
        settings=platform.settings,
        workspace_id=loaded.workspace_id,
        experiment_id=loaded.experiment_id,
        budget_id=loaded.budget_id,
        attempt_id=loaded.attempt_id,
        price=loaded.price,
        recorder=recorder,
        lease_token=loaded.lease_token,
    )
    async with models_for_attempt:
        try:
            link = (
                await _gateway_link(platform, loaded)
                if loaded.adapter_type == models.AdapterType.HTTP
                else None
            )
            adapter = build_adapter(
                loaded.adapter_type, loaded.adapter_settings, models_for_attempt, link
            )
            async with asyncio.timeout(loaded.limits.timeout_seconds):
                return await adapter.run(agent_input, tools, recorder, loaded.limits)
        except TimeoutError:
            return AgentResult("", "timeout")
        except AttemptNotLiveError:
            raise
        except BudgetExhaustedError:
            return AgentResult("", "budget")
        except ContentBlocked as exc:
            # The model refused to answer. That is the agent's behavior, so it is graded.
            return AgentResult("", "agent_error", {"error": "content_blocked", "detail": str(exc)})
        except _MODEL_UNAVAILABLE as exc:
            logger.warning("model unavailable", extra={"attempt_id": loaded.attempt_id})
            raise _ModelUnavailableError(f"{type(exc).__name__}: {exc}"[:300]) from exc
        except Exception as exc:
            # The agent's own failure is a result to grade, not a platform error.
            logger.warning("agent raised", exc_info=True, extra={"attempt_id": loaded.attempt_id})
            return AgentResult("", "agent_error", {"error": type(exc).__name__})


async def _gateway_link(platform: Platform, loaded: _Loaded) -> GatewayLink:
    settings = platform.settings
    async with platform.sessionmaker() as session:
        secret = await signing_secret(
            session, loaded.workspace_id, key_vault_from_settings(settings)
        )
    return GatewayLink(
        sessionmaker=platform.sessionmaker,
        workspace_id=loaded.workspace_id,
        attempt_id=loaded.attempt_id,
        lease_token=loaded.lease_token,
        gateway_url=settings.gateway_public_url,
        signing_secret=secret,
        policy=GuardPolicy.of(settings),
    )


async def _hit_tool_limit(platform: Platform, loaded: _Loaded) -> bool:
    async with platform.sessionmaker() as session:
        scope = WorkspaceScope(session, loaded.workspace_id)
        return await count_limit_refusals(scope, loaded.attempt_id) > 0


async def _evaluate(
    platform: Platform, loaded: _Loaded, final_state: dict[str, Any], final_response: str
) -> list[Score]:
    async with platform.sessionmaker() as session:
        rows = await trace_events(WorkspaceScope(session, loaded.workspace_id), loaded.attempt_id)
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


async def _load_and_mark_running(
    platform: Platform, workspace_id: str, attempt_id: str, lease_token: str | None
) -> _Loaded:
    async with platform.sessionmaker.begin() as session:
        rows = await load_attempt(WorkspaceScope(session, workspace_id), attempt_id)
        attempt, run, task, config = rows.attempt, rows.run, rows.task, rows.config
        if attempt.execution_status != models.ExecutionStatus.QUEUED:
            raise AttemptNotLiveError(f"{attempt_id} is {attempt.execution_status}, not queued")
        token = lease_token or attempt.lease_token
        if token is None:
            raise AttemptNotLiveError(attempt_id)
        await require_live(session, workspace_id, attempt_id, token)
        now = datetime.now(UTC)
        attempt.execution_status = models.ExecutionStatus.RUNNING
        attempt.started_at = now
        run.execution_status = models.ExecutionStatus.RUNNING
        experiment = rows.experiment
        if experiment.status == models.ExperimentStatus.QUEUED:
            experiment.status = models.ExperimentStatus.RUNNING
        budget = await budget_for_experiment(WorkspaceScope(session, workspace_id), experiment.id)
        return _Loaded(
            attempt_id=attempt.id,
            workspace_id=workspace_id,
            run_id=run.id,
            experiment_id=run.experiment_id,
            env_id=attempt.env_id,
            lease_token=token,
            budget_id=budget.id,
            task=TaskVersion(
                id=task.id,
                task_key=task.task_key,
                environment=task.environment,
                scenario_family=task.scenario_family,
                split=task.split,
                agent_input=AgentTaskInput.model_validate(task.agent_input),
                fixture=task.fixture,
                evaluator_spec=task.evaluator_spec,
                faults=tuple(FaultSpec.model_validate(f) for f in task.faults),
            ),
            agent_config_id=config.id,
            adapter_type=config.adapter_type,
            adapter_settings=config.settings,
            limits=RunLimits.model_validate(config.limits).tighter(
                RunLimits.model_validate(task.limits)
            ),
            price=experiment.manifest.get("prices", {}).get(config.id),
        )


def _save_snapshot(
    scope: WorkspaceScope, loaded: _Loaded, kind: models.SnapshotKind, state: dict[str, Any]
) -> None:
    scope.add(
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
    execution_status: models.ExecutionStatus,
    started: float,
) -> None:
    async with platform.sessionmaker.begin() as session:
        # The final write is fenced like every other: a stale worker records no result.
        await require_live(session, loaded.workspace_id, loaded.attempt_id, loaded.lease_token)
        scope = WorkspaceScope(session, loaded.workspace_id)
        for score in scores:
            scope.add(
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
        attempt = await scope.require(models.Attempt, loaded.attempt_id)
        attempt.execution_status = execution_status
        attempt.stop_reason = result.stop_reason
        attempt.final_response = result.final_response
        attempt.finished_at = datetime.now(UTC)
        attempt.duration_ms = round((time.monotonic() - started) * 1000)
        attempt.lease_token = None
        run = await scope.require(models.Run, loaded.run_id)
        run.execution_status = execution_status
        run.evaluation_status = status
        await finish_job(scope, loaded.run_id)
        await scope.flush()
        await complete_if_done(scope, loaded.experiment_id)


async def _mark_infrastructure_error(platform: Platform, loaded: _Loaded, started: float) -> None:
    async with platform.sessionmaker.begin() as session:
        try:
            await require_live(session, loaded.workspace_id, loaded.attempt_id, loaded.lease_token)
        except AttemptNotLiveError:
            return
        scope = WorkspaceScope(session, loaded.workspace_id)
        attempt = await scope.require(models.Attempt, loaded.attempt_id)
        attempt.execution_status = models.ExecutionStatus.INFRASTRUCTURE_ERROR
        attempt.finished_at = datetime.now(UTC)
        attempt.duration_ms = round((time.monotonic() - started) * 1000)
        attempt.lease_token = None
        run = await scope.require(models.Run, loaded.run_id)
        run.execution_status = models.ExecutionStatus.INFRASTRUCTURE_ERROR
        await finish_job(scope, loaded.run_id)
