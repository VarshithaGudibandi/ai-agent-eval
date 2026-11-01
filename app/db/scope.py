from typing import Any, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models

Row = TypeVar("Row", bound=models.WorkspaceScoped)


class NotFoundError(LookupError):
    """The row does not exist in this workspace. Rows in other workspaces look the same."""


class CrossWorkspaceError(ValueError):
    pass


class WorkspaceScope:
    """The only way code reads or writes workspace data.

    Every query built here is filtered by the workspace, so a caller cannot forget the
    filter. Rows from another workspace are indistinguishable from rows that do not exist.
    """

    def __init__(self, session: AsyncSession, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def query(self, model: type[Row]) -> Select[Row]:
        return select(model).where(model.workspace_id == self.workspace_id)

    async def get(self, model: type[Row], row_id: str) -> Row | None:
        id_column: Any = model.id  # type: ignore[attr-defined]
        row: Row | None = await self.session.scalar(self.query(model).where(id_column == row_id))
        return row

    async def require(self, model: type[Row], row_id: str) -> Row:
        row = await self.get(model, row_id)
        if row is None:
            raise NotFoundError(f"{model.__tablename__}:{row_id}")  # type: ignore[attr-defined]
        return row

    async def all(self, statement: Select[Row]) -> list[Row]:
        return list(await self.session.scalars(statement))

    async def first(self, statement: Select[Row]) -> Row | None:
        row: Row | None = await self.session.scalar(statement.limit(1))
        return row

    def add(self, row: models.WorkspaceScoped) -> None:
        if row.workspace_id != self.workspace_id:
            raise CrossWorkspaceError(
                f"row for workspace {row.workspace_id!r} added in {self.workspace_id!r}"
            )
        self.session.add(row)

    async def flush(self) -> None:
        await self.session.flush()
