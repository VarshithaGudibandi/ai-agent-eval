from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Request
from pydantic import BaseModel, Field

from app.db.run_tokens import InvalidRunTokenError, post_external_result, resolve_run_token
from app.environments.contract import FaultSpec
from app.execution.trace_recorder import TraceRecorder
from app.gateway.contract import ToolCallOutcome
from app.gateway.tool_gateway import AttemptContext, ToolGateway

router = APIRouter(prefix="/gw/v1", tags=["tool gateway"])


def _token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise InvalidRunTokenError("missing bearer run token")
    return token


class AgentResultIn(BaseModel):
    final_response: str = Field(max_length=100_000)
    usage: dict[str, Any] | None = None


@router.post("/attempts/{attempt_id}/tools/{tool}")
async def call_tool(
    attempt_id: str,
    tool: str,
    request: Request,
    arguments: Annotated[dict[str, Any], Body()],
    authorization: Annotated[str | None, Header()] = None,
) -> ToolCallOutcome:
    """Call one tool for an external agent. Accepts only the attempt's run token.

    This runs the same gateway as the built-in agent, so every call is validated, fenced
    and traced the same way.
    """
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        access = await resolve_run_token(session, attempt_id, _token(authorization))
    ctx = AttemptContext(
        attempt_id=access.attempt_id,
        workspace_id=access.workspace_id,
        env_id=access.env_id,
        environment=access.environment,
        task_context=access.task_context,
        faults=tuple(FaultSpec.model_validate(f) for f in access.faults),
        max_tool_calls=access.max_tool_calls,
    )
    recorder = TraceRecorder(
        sessionmaker, access.workspace_id, access.attempt_id, access.lease_token
    )
    gateway: ToolGateway = request.app.state.gateway
    return await gateway.call(ctx, recorder, tool, arguments)


@router.post("/attempts/{attempt_id}/result")
async def post_result(
    attempt_id: str,
    body: AgentResultIn,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    """Where an agent that answered 202 posts its final answer."""
    async with request.app.state.sessionmaker.begin() as session:
        access = await resolve_run_token(session, attempt_id, _token(authorization))
        await post_external_result(session, access, body.model_dump())
    return {"accepted": True}
