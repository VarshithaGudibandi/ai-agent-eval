from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.auth.users import create_user
from app.auth.workspaces import create_workspace
from app.db import models
from app.db.ids import new_id


async def owner_with_workspace(session: AsyncSession) -> tuple[models.User, Principal]:
    user = await create_user(session, f"{new_id('u')}@example.com", "a long password")
    workspace = await create_workspace(session, user, "team")
    principal = Principal(
        actor_type=models.ActorType.USER,
        actor_id=user.id,
        user_id=user.id,
        workspace_id=workspace.id,
        role=models.Role.OWNER,
    )
    return user, principal
