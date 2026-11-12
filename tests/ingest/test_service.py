import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.scope import WorkspaceScope
from app.ingest import service
from app.ingest.service import ingest
from tests.ingest.sample_traces import support_trace

Sessions = async_sessionmaker[AsyncSession]


async def test_a_trace_that_cannot_be_normalized_is_still_kept_raw(
    sessionmaker: Sessions, workspace_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object) -> None:
        raise RuntimeError("normalizer bug")

    monkeypatch.setattr(service, "normalize", broken)
    async with sessionmaker.begin() as session:
        result = await ingest(WorkspaceScope(session, workspace_id), [support_trace()])
    assert result.accepted == ["trace-001"]
    async with sessionmaker() as session:
        trace = await session.scalar(
            select(models.Trace).where(models.Trace.workspace_id == workspace_id)
        )
        runs = list(
            await session.scalars(select(models.Run).where(models.Run.workspace_id == workspace_id))
        )
        versions = list(
            await session.scalars(
                select(models.AgentVersion).where(models.AgentVersion.workspace_id == workspace_id)
            )
        )
    assert trace is not None
    assert (trace.status, trace.error) == ("failed", "RuntimeError: normalizer bug")
    assert trace.payload["trace_id"] == "trace-001"
    # The savepoint undid everything the failed step wrote, so no half-built run is left.
    assert runs == [] and versions == []


async def test_a_trace_sent_twice_in_one_batch_counts_once(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        result = await ingest(
            WorkspaceScope(session, workspace_id), [support_trace(), support_trace()]
        )
    assert (result.accepted, result.duplicates) == (["trace-001"], ["trace-001"])
