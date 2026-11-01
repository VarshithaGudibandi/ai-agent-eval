import logging
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.ids import new_id
from app.environments.contract import CallContext, ToolResult, ToolSchema
from app.environments.registry import EnvironmentRegistry
from app.environments.state_store import EnvStateStore
from app.execution.trace_recorder import TraceRecorder
from app.gateway.contract import PublicToolSchema, ToolCallOutcome

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


def _rejected(tool_call_id: str, code: str, detail: Any) -> tuple[ToolCallOutcome, list[Any]]:
    outcome = ToolCallOutcome(
        tool_call_id=tool_call_id, status="rejected", error_code=code, output={"detail": detail}
    )
    return outcome, []


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
                outcome, changes = await self._execute(session, ctx, tool_call_id, tool, arguments)
                await self._record_result(
                    session,
                    ctx,
                    recorder,
                    request_event_id,
                    tool,
                    arguments,
                    outcome,
                    changes,
                    started,
                )
        except Exception:
            logger.exception("tool execution failed", extra={"tool": tool})
            outcome = ToolCallOutcome(
                tool_call_id=tool_call_id,
                status="error",
                error_code="TOOL_FAILED",
                output={"message": "The tool failed. No changes were made."},
            )
            async with self._sessionmaker.begin() as session:
                await self._record_result(
                    session,
                    ctx,
                    recorder,
                    request_event_id,
                    tool,
                    arguments,
                    outcome,
                    [],
                    started,
                )
        return outcome

    async def _execute(
        self,
        session: AsyncSession,
        ctx: AttemptContext,
        tool_call_id: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> tuple[ToolCallOutcome, list[Any]]:
        env = self._environments.create(ctx.environment, EnvStateStore(session, ctx.workspace_id))
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
        call_ctx = CallContext(
            attempt_id=ctx.attempt_id,
            workspace_id=ctx.workspace_id,
            env_id=ctx.env_id,
            tool_call_id=tool_call_id,
            task_context=ctx.task_context,
        )
        result: ToolResult = await env.execute(ctx.env_id, tool, args, call_ctx)
        outcome = ToolCallOutcome(
            tool_call_id=tool_call_id,
            status=result.status,
            output=result.output,
            error_code=result.error_code,
        )
        return outcome, [c.model_dump() for c in result.state_changes]

    async def _record_result(
        self,
        session: AsyncSession,
        ctx: AttemptContext,
        recorder: TraceRecorder,
        request_event_id: str,
        tool: str,
        arguments: dict[str, Any],
        outcome: ToolCallOutcome,
        changes: list[Any],
        started: float,
    ) -> None:
        duration_ms = round((time.monotonic() - started) * 1000)
        result_event_id = await recorder.record_in(
            session,
            "tool_call_completed",
            {
                **outcome.model_dump(),
                "tool": tool,
                "state_changes": changes,
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
