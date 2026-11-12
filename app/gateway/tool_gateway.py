import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.attempts import AttemptNotLiveError
from app.db.hashing import content_hash
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.db.tool_calls import TOOL_LIMIT_REACHED, count_all_tool_calls, count_tool_calls
from app.environments.contract import CallContext, FaultSpec, ToolArgs, ToolResult, ToolSchema
from app.environments.registry import EnvironmentRegistry
from app.environments.state_store import EnvStateStore
from app.execution.trace_recorder import TraceRecorder
from app.gateway.contract import ToolCallOutcome
from app.providers.contract import PublicToolSchema

logger = logging.getLogger(__name__)


class AttemptContext(BaseModel):
    """What the server knows about an attempt. Identity comes from here, never the agent."""

    model_config = ConfigDict(frozen=True)

    attempt_id: str
    workspace_id: str
    env_id: str
    environment: str
    task_context: dict[str, Any]
    allowed_tools: frozenset[str] | None = None
    faults: tuple[FaultSpec, ...] = ()
    # Enforced here rather than trusted to the agent, because an external agent can call the
    # gateway as often as it likes.
    max_tool_calls: int | None = None


# Successful results of state-changing calls, kept with the attempt's environment state so
# a retry returns the first result instead of applying the effect again.
REPLAYS = "_replays"

_FAULT_ERRORS = {
    "timeout": ("TIMEOUT", "The service did not answer in time."),
    "service_error": ("SERVICE_UNAVAILABLE", "The service is temporarily unavailable."),
}


def idempotency_key(attempt_id: str, tool: str, args: ToolArgs) -> str:
    return content_hash(
        {"attempt_id": attempt_id, "tool": tool, "arguments": args.model_dump(mode="json")}
    )


@dataclass
class _Executed:
    outcome: ToolCallOutcome
    changes: list[dict[str, Any]] = field(default_factory=list)
    fault: FaultSpec | None = None
    replayed: bool = False


def allowed_tools(
    schemas: list[ToolSchema], allowed: frozenset[str] | None
) -> dict[str, ToolSchema]:
    return {s.name: s for s in schemas if allowed is None or s.name in allowed}


def public_schemas(
    schemas: list[ToolSchema], allowed: frozenset[str] | None
) -> list[PublicToolSchema]:
    return [
        PublicToolSchema(name=s.name, description=s.description, parameters=s.parameters())
        for s in allowed_tools(schemas, allowed).values()
    ]


def _rejected(tool_call_id: str, code: str, detail: Any) -> _Executed:
    return _Executed(
        ToolCallOutcome(
            tool_call_id=tool_call_id, status="rejected", error_code=code, output={"detail": detail}
        )
    )


def _fault_outcome(tool_call_id: str, fault: FaultSpec) -> ToolCallOutcome:
    code, message = _FAULT_ERRORS[fault.kind]
    return ToolCallOutcome(
        tool_call_id=tool_call_id, status="error", error_code=code, output={"message": message}
    )


class ToolGateway:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], environments: EnvironmentRegistry
    ) -> None:
        self._sessionmaker = sessionmaker
        self._environments = environments

    async def call(
        self,
        ctx: AttemptContext,
        recorder: TraceRecorder,
        tool: str,
        arguments: dict[str, Any],
    ) -> ToolCallOutcome:
        tool_call_id = new_id("tc")
        # The request is committed on its own first, so it survives even if execution fails.
        request_event_id = await recorder.record(
            "tool_call_requested",
            {"tool_call_id": tool_call_id, "tool": tool, "arguments": arguments},
        )
        started = time.monotonic()
        try:
            async with self._sessionmaker.begin() as session:
                executed = await self._execute(session, ctx, tool_call_id, tool, arguments)
                await self._record_result(
                    session, ctx, recorder, request_event_id, tool, arguments, executed, started
                )
        except AttemptNotLiveError:
            # A stale worker's call: its effects were rolled back and nothing is recorded.
            raise
        except Exception:
            logger.exception("tool execution failed", extra={"tool": tool})
            outcome = ToolCallOutcome(
                tool_call_id=tool_call_id,
                status="error",
                error_code="TOOL_FAILED",
                output={"message": "The tool failed. No changes were made."},
            )
            executed = _Executed(outcome)
            async with self._sessionmaker.begin() as session:
                await self._record_result(
                    session, ctx, recorder, request_event_id, tool, arguments, executed, started
                )
        return executed.outcome

    async def _execute(
        self,
        session: AsyncSession,
        ctx: AttemptContext,
        tool_call_id: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> _Executed:
        scope = WorkspaceScope(session, ctx.workspace_id)
        if ctx.max_tool_calls is not None and (
            await count_all_tool_calls(scope, ctx.attempt_id) >= ctx.max_tool_calls
        ):
            return _rejected(
                tool_call_id, TOOL_LIMIT_REACHED, f"The run allows {ctx.max_tool_calls} calls."
            )
        store = EnvStateStore(session, ctx.workspace_id)
        env = self._environments.create(ctx.environment, store)
        schema = allowed_tools(env.tool_schemas(), ctx.allowed_tools).get(tool)
        if schema is None:
            return _rejected(tool_call_id, "TOOL_NOT_ALLOWED", f"No tool named {tool!r}.")
        try:
            args = schema.args_model.model_validate(arguments)
        except ValidationError as exc:
            return _rejected(
                tool_call_id,
                "INVALID_ARGUMENTS",
                exc.errors(include_url=False, include_input=False, include_context=False),
            )
        occurrence = await count_tool_calls(scope, ctx.attempt_id, tool) + 1
        fault = next((f for f in ctx.faults if f.tool == tool and f.occurrence == occurrence), None)
        if fault is not None and fault.when == "before_effect":
            return _Executed(_fault_outcome(tool_call_id, fault), fault=fault)
        key = idempotency_key(ctx.attempt_id, tool, args)
        stored = await store.get(ctx.env_id, REPLAYS, key) if schema.effectful else None
        if stored is not None:
            result = ToolResult.model_validate(stored)
        else:
            call_ctx = CallContext(
                attempt_id=ctx.attempt_id,
                workspace_id=ctx.workspace_id,
                env_id=ctx.env_id,
                tool_call_id=tool_call_id,
                task_context=ctx.task_context,
                idempotency_key=key,
            )
            result = await env.execute(ctx.env_id, tool, args, call_ctx)
            # Only successes are kept: a refused call changed nothing, and the same call may
            # succeed later, for example once its approval exists.
            if schema.effectful and result.status == "ok":
                await store.put(
                    ctx.env_id, REPLAYS, key, result.model_dump(exclude={"state_changes"})
                )
        outcome = ToolCallOutcome(
            tool_call_id=tool_call_id,
            status=result.status,
            output=result.output,
            error_code=result.error_code,
        )
        if fault is not None:
            outcome = _fault_outcome(tool_call_id, fault)
        return _Executed(
            outcome,
            [c.model_dump() for c in result.state_changes],
            fault=fault,
            replayed=stored is not None,
        )

    async def _record_result(
        self,
        session: AsyncSession,
        ctx: AttemptContext,
        recorder: TraceRecorder,
        request_event_id: str,
        tool: str,
        arguments: dict[str, Any],
        executed: _Executed,
        started: float,
    ) -> None:
        outcome = executed.outcome
        duration_ms = round((time.monotonic() - started) * 1000)
        # The fault and the real state change go in the trace for the evaluators. The agent
        # only ever sees the outcome.
        result_event_id = await recorder.record_in(
            session,
            "tool_call_completed",
            {
                **outcome.model_dump(),
                "tool": tool,
                "state_changes": executed.changes,
                "fault": executed.fault.model_dump() if executed.fault else None,
                "replayed": executed.replayed,
                "duration_ms": duration_ms,
            },
        )
        session.add(
            models.ToolCall(
                id=outcome.tool_call_id,
                workspace_id=ctx.workspace_id,
                attempt_id=ctx.attempt_id,
                request_event_id=request_event_id,
                result_event_id=result_event_id,
                tool_name=tool,
                arguments=arguments,
                status=outcome.status,
                error_code=outcome.error_code,
                output=outcome.output,
                duration_ms=duration_ms,
            )
        )


class InProcessToolSession:
    """Lets an agent in this process call the gateway as if it were remote."""

    def __init__(
        self,
        gateway: ToolGateway,
        ctx: AttemptContext,
        recorder: TraceRecorder,
        schemas: list[PublicToolSchema],
    ) -> None:
        self._gateway = gateway
        self._ctx = ctx
        self._recorder = recorder
        self._schemas = schemas

    @property
    def schemas(self) -> list[PublicToolSchema]:
        return list(self._schemas)

    async def call(self, tool: str, arguments: dict[str, Any]) -> ToolCallOutcome:
        return await self._gateway.call(self._ctx, self._recorder, tool, arguments)
