import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.contract import RunLimits
from app.db import models
from app.db.attempts import AttemptNotLiveError, load_attempt, require_live
from app.db.scope import WorkspaceScope


class InvalidRunTokenError(Exception):
    """No attempt has this token, or it expired. Answers the same either way."""

    code = "invalid_run_token"


class ResultAlreadyPostedError(Exception):
    code = "result_already_posted"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_run_token(
    session: AsyncSession, workspace_id: str, attempt_id: str, lease_token: str, seconds: float
) -> str:
    """Give a live attempt a token an external agent can call the gateway with."""
    await require_live(session, workspace_id, attempt_id, lease_token)
    attempt = await WorkspaceScope(session, workspace_id).require(models.Attempt, attempt_id)
    token = f"run_{secrets.token_urlsafe(32)}"
    attempt.run_token_hash = _hash(token)
    attempt.run_token_expires_at = func.now() + timedelta(seconds=seconds)
    return token


@dataclass(frozen=True)
class RunAccess:
    """What the gateway needs to serve one external agent's call."""

    workspace_id: str
    attempt_id: str
    env_id: str
    environment: str
    task_context: dict[str, Any]
    faults: list[dict[str, Any]]
    max_tool_calls: int
    lease_token: str


async def resolve_run_token(session: AsyncSession, attempt_id: str, token: str) -> RunAccess:
    """Check a run token against its attempt, from any workspace: the token is the credential.

    A token is good only for its own attempt, only until it expires, and only while the
    attempt's lease holds. Expired and unknown tokens answer alike; a valid token on an
    attempt that is no longer live gets attempt_not_live.
    """
    attempt = await session.scalar(
        select(models.Attempt).where(
            models.Attempt.id == attempt_id,
            models.Attempt.run_token_expires_at > func.now(),
        )
    )
    if (
        attempt is None
        or attempt.run_token_hash is None
        or not hmac.compare_digest(attempt.run_token_hash, _hash(token))
    ):
        raise InvalidRunTokenError(attempt_id)
    if attempt.lease_token is None:
        raise AttemptNotLiveError(attempt_id)
    await require_live(session, attempt.workspace_id, attempt.id, attempt.lease_token)
    rows = await load_attempt(WorkspaceScope(session, attempt.workspace_id), attempt.id)
    limits = RunLimits.model_validate(rows.config.limits).tighter(
        RunLimits.model_validate(rows.task.limits)
    )
    return RunAccess(
        workspace_id=attempt.workspace_id,
        attempt_id=attempt.id,
        env_id=attempt.env_id,
        environment=rows.task.environment,
        task_context=rows.task.agent_input.get("context", {}),
        faults=rows.task.faults,
        max_tool_calls=limits.max_tool_calls,
        lease_token=attempt.lease_token,
    )


async def post_external_result(
    session: AsyncSession, access: RunAccess, result: dict[str, Any]
) -> None:
    """Store the answer an agent posts after replying 202. One answer per attempt."""
    await require_live(session, access.workspace_id, access.attempt_id, access.lease_token)
    attempt = await WorkspaceScope(session, access.workspace_id).require(
        models.Attempt, access.attempt_id
    )
    if attempt.external_result is not None:
        raise ResultAlreadyPostedError(access.attempt_id)
    attempt.external_result = result


async def external_result(
    session: AsyncSession, workspace_id: str, attempt_id: str
) -> dict[str, Any] | None:
    attempt = await WorkspaceScope(session, workspace_id).require(models.Attempt, attempt_id)
    return attempt.external_result
