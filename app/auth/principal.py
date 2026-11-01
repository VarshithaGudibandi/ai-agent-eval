from pydantic import BaseModel, ConfigDict

from app.db.models import ActorType, Role

_RANK = {Role.VIEWER: 0, Role.MEMBER: 1, Role.OWNER: 2}


def role_at_least(role: Role, minimum: Role) -> bool:
    return _RANK[role] >= _RANK[minimum]


class Principal(BaseModel):
    """Who is calling and in which workspace. Every request has exactly one."""

    model_config = ConfigDict(frozen=True)

    actor_type: ActorType
    actor_id: str
    user_id: str | None
    workspace_id: str
    role: Role
    session_id: str | None = None
