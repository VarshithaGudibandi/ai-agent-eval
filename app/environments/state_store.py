from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models


class EnvStateStore:
    """Environment state kept in Postgres, so any worker can resume or inspect an attempt.

    Every environment stores its records here instead of in tables of its own, so adding
    an environment never needs a migration.
    """

    def __init__(self, session: AsyncSession, workspace_id: str) -> None:
        self._session = session
        self._workspace_id = workspace_id

    async def put(self, env_id: str, collection: str, record_id: str, data: dict[str, Any]) -> None:
        statement = insert(models.EnvRecord).values(
            workspace_id=self._workspace_id,
            env_id=env_id,
            collection=collection,
            record_id=record_id,
            data=data,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=["env_id", "collection", "record_id"],
                set_={"data": statement.excluded.data},
            )
        )

    async def get(
        self, env_id: str, collection: str, record_id: str, *, for_update: bool = False
    ) -> dict[str, Any] | None:
        query = select(models.EnvRecord.data).where(
            models.EnvRecord.workspace_id == self._workspace_id,
            models.EnvRecord.env_id == env_id,
            models.EnvRecord.collection == collection,
            models.EnvRecord.record_id == record_id,
        )
        if for_update:
            query = query.with_for_update()
        return (await self._session.execute(query)).scalar_one_or_none()

    async def list_records(self, env_id: str, collection: str) -> list[dict[str, Any]]:
        query = (
            select(models.EnvRecord.data)
            .where(
                models.EnvRecord.workspace_id == self._workspace_id,
                models.EnvRecord.env_id == env_id,
                models.EnvRecord.collection == collection,
            )
            .order_by(models.EnvRecord.record_id)
        )
        return list((await self._session.execute(query)).scalars())

    async def snapshot(self, env_id: str) -> dict[str, list[dict[str, Any]]]:
        query = (
            select(models.EnvRecord.collection, models.EnvRecord.data)
            .where(
                models.EnvRecord.workspace_id == self._workspace_id,
                models.EnvRecord.env_id == env_id,
            )
            .order_by(models.EnvRecord.collection, models.EnvRecord.record_id)
        )
        state: dict[str, list[dict[str, Any]]] = {}
        for collection, data in await self._session.execute(query):
            state.setdefault(collection, []).append(data)
        return state

    async def clear(self, env_id: str) -> None:
        await self._session.execute(
            delete(models.EnvRecord).where(
                models.EnvRecord.workspace_id == self._workspace_id,
                models.EnvRecord.env_id == env_id,
            )
        )
