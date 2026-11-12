import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.eval_specs import current_spec, scorers_of
from app.db.ids import new_id
from app.db.jobs import LeaseLostError, cancel, claim_next, finish
from app.db.scope import WorkspaceScope
from app.db.traces import agent_version
from app.ingest.redact import redact_value
from app.ingest.service import ingest
from app.jobs.worker import JobContext
from app.scoring.definitions import ScorerDefinition
from app.scoring.job import SCORE_RUN, queue_scoring, score_run_job
from app.scoring.specs import save_definitions
from app.traces.normalize import fingerprint
from tests.scoring.golden import golden

Sessions = async_sessionmaker[AsyncSession]


async def _workspace(sessions: Sessions) -> str:
    workspace = models.Workspace(id=new_id("ws"), name="scoring")
    async with sessions.begin() as session:
        session.add(workspace)
    return workspace.id


def _trace(case: str = "refund") -> dict[str, Any]:
    trace = golden(case)
    trace["trace_id"] = uuid.uuid4().hex
    return trace


async def _ingest(sessions: Sessions, workspace_id: str, *traces: dict[str, Any]) -> list[str]:
    async with sessions.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        await ingest(scope, list(traces))
        runs = await scope.all(scope.query(models.Run).order_by(models.Run.created_at))
    return [r.id for r in runs]


async def _work_one(sessions: Sessions) -> None:
    async with sessions.begin() as session:
        claim = await claim_next(session, "test", 30, [SCORE_RUN])
    assert claim is not None
    await score_run_job(JobContext(claim, sessions))
    async with sessions.begin() as session:
        await finish(session, claim.workspace_id, claim.job_id, claim.lease_token)


async def _verdicts(sessions: Sessions, run_id: str) -> list[tuple[int, str, list[str]]]:
    async with sessions() as session:
        rows = await session.execute(
            select(models.EvalSpec.version, models.Verdict.status, models.Verdict.failed)
            .join(models.EvalSpec, models.EvalSpec.id == models.Verdict.eval_spec_id)
            .where(models.Verdict.run_id == run_id)
            .order_by(models.EvalSpec.version)
        )
        return [tuple(r) for r in rows.all()]  # type: ignore[misc]


async def _score_count(sessions: Sessions, run_id: str) -> int:
    async with sessions() as session:
        count = await session.scalar(
            select(func.count()).select_from(models.Score).where(models.Score.run_id == run_id)
        )
    return int(count or 0)


async def test_a_new_run_is_scored_by_the_default_preset_once(fresh_sessionmaker: Sessions) -> None:
    workspace_id = await _workspace(fresh_sessionmaker)
    [run_id] = await _ingest(fresh_sessionmaker, workspace_id, _trace())
    await _work_one(fresh_sessionmaker)
    assert await _verdicts(fresh_sessionmaker, run_id) == [(1, "passed", [])]
    # One run-level score each for two scorers, and one per tool call for the other two.
    assert await _score_count(fresh_sessionmaker, run_id) == 1 + 1 + 2 + 2
    async with fresh_sessionmaker() as session:
        scope = WorkspaceScope(session, workspace_id)
        agent = await scope.first(scope.query(models.Agent))
        assert agent is not None
        spec = await current_spec(scope, agent.id)
        assert spec is not None
        assert [s.key for s in await scorers_of(scope, spec.id)] == [
            "run_completed",
            "tool_errors",
            "valid_tool_arguments",
            "turn_limit",
        ]
    # Scoring the same run again adds nothing.
    async with fresh_sessionmaker.begin() as session:
        assert await queue_scoring(WorkspaceScope(session, workspace_id), run_id)
    await _work_one(fresh_sessionmaker)
    assert await _score_count(fresh_sessionmaker, run_id) == 6
    assert len(await _verdicts(fresh_sessionmaker, run_id)) == 1


async def test_a_run_is_graded_by_the_spec_in_force_when_it_arrived(
    fresh_sessionmaker: Sessions,
) -> None:
    workspace_id = await _workspace(fresh_sessionmaker)
    [run_id] = await _ingest(fresh_sessionmaker, workspace_id, _trace())
    stricter = [ScorerDefinition(key="thanks", check="reply_contains", params={"text": "thank"})]
    async with fresh_sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        run = await scope.require(models.Run, run_id)
        version = await scope.require(models.AgentVersion, run.agent_version_id)
        new_spec, created = await save_definitions(scope, version.agent_id, stricter)
        assert created and new_spec.version == 2
    await _work_one(fresh_sessionmaker)
    assert await _verdicts(fresh_sessionmaker, run_id) == [(1, "passed", [])]
    # Re-scoring under the new version keeps the old verdict beside the new one.
    async with fresh_sessionmaker.begin() as session:
        await queue_scoring(WorkspaceScope(session, workspace_id), run_id, new_spec.id)
    await _work_one(fresh_sessionmaker)
    assert await _verdicts(fresh_sessionmaker, run_id) == [
        (1, "passed", []),
        (2, "failed", ["thanks"]),
    ]


async def test_a_worker_that_lost_its_lease_stores_nothing(fresh_sessionmaker: Sessions) -> None:
    workspace_id = await _workspace(fresh_sessionmaker)
    [run_id] = await _ingest(fresh_sessionmaker, workspace_id, _trace())
    async with fresh_sessionmaker.begin() as session:
        claim = await claim_next(session, "stale", 30, [SCORE_RUN])
    assert claim is not None
    async with fresh_sessionmaker.begin() as session:
        await cancel(WorkspaceScope(session, workspace_id), run_id)
    with pytest.raises(LeaseLostError):
        await score_run_job(JobContext(claim, fresh_sessionmaker))
    assert await _score_count(fresh_sessionmaker, run_id) == 0
    assert await _verdicts(fresh_sessionmaker, run_id) == []


async def test_traces_arriving_together_for_an_agent_without_a_spec_make_one(
    fresh_sessionmaker: Sessions,
) -> None:
    # Agents made before scoring existed have no spec until their next trace.
    workspace_id = await _workspace(fresh_sessionmaker)
    trace = _trace()
    async with fresh_sessionmaker.begin() as session:
        await agent_version(
            WorkspaceScope(session, workspace_id),
            trace["agent"],
            fingerprint(redact_value(trace["config"])),
            trace["config"],
            None,
        )
    await asyncio.gather(*(_ingest(fresh_sessionmaker, workspace_id, _trace()) for _ in range(4)))
    async with fresh_sessionmaker() as session:
        specs = await session.scalar(select(func.count()).select_from(models.EvalSpec))
        jobs = await session.scalar(select(func.count()).select_from(models.Job))
    assert (specs, jobs) == (1, 4)
