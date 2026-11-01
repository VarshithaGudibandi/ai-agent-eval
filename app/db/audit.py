from typing import Any

from app.db import models
from app.db.ids import new_id
from app.db.scope import WorkspaceScope


def record_audit(
    scope: WorkspaceScope,
    actor_type: models.ActorType,
    actor_id: str,
    action: str,
    target_type: str,
    target_id: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    scope.add(
        models.AuditEntry(
            id=new_id("aud"),
            workspace_id=scope.workspace_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details or {},
        )
    )
