from sqlalchemy import func, select

from app.db import models
from app.db.scope import WorkspaceScope

TOOL_LIMIT_REACHED = "TOOL_LIMIT_REACHED"


async def count_tool_calls(scope: WorkspaceScope, attempt_id: str, tool: str) -> int:
    """Calls of one tool in an attempt that got past validation, failed ones included."""
    count = await scope.session.scalar(
        select(func.count())
        .select_from(models.ToolCall)
        .where(
            models.ToolCall.workspace_id == scope.workspace_id,
            models.ToolCall.attempt_id == attempt_id,
            models.ToolCall.tool_name == tool,
            models.ToolCall.status != models.ToolCallStatus.REJECTED,
        )
    )
    return count or 0


async def count_limit_refusals(scope: WorkspaceScope, attempt_id: str) -> int:
    """Calls the gateway refused because the run had used up its tool calls."""
    count = await scope.session.scalar(
        select(func.count())
        .select_from(models.ToolCall)
        .where(
            models.ToolCall.workspace_id == scope.workspace_id,
            models.ToolCall.attempt_id == attempt_id,
            models.ToolCall.error_code == TOOL_LIMIT_REACHED,
        )
    )
    return count or 0


async def count_all_tool_calls(scope: WorkspaceScope, attempt_id: str) -> int:
    count = await scope.session.scalar(
        select(func.count())
        .select_from(models.ToolCall)
        .where(
            models.ToolCall.workspace_id == scope.workspace_id,
            models.ToolCall.attempt_id == attempt_id,
        )
    )
    return count or 0
