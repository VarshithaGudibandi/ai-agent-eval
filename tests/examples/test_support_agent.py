import asyncio
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_eval import Eval
from app.db import models
from examples.support_agent.agent import CONFIG, SupportAgent
from examples.support_agent.eval import DATASET, SCORERS
from tests.conftest import LivePlatform
from tests.examples.brains import careful
from tests.sdk.fake_gemini import FakeGemini


async def test_the_example_agent_runs_eval_and_every_run_arrives(
    live_platform: LivePlatform,
    member_token: tuple[str, str],
    fresh_sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    token, _ = member_token
    agent = SupportAgent(FakeGemini(careful))
    result = await asyncio.to_thread(
        Eval,
        agent="support-agent",
        task=agent.handle,
        scorers=SCORERS,
        dataset=DATASET,
        config=CONFIG,
        repetitions=2,
        url=live_platform.url,
        token=token,
        pending_dir=tmp_path / "pending",
    )
    assert (result.expected_runs, result.runs_received, result.task_errors) == (16, 16, 0)
    # A careful agent passes the built-ins and the refund policy on every case.
    assert (result.runs_scored, result.runs_passed, result.failures) == (16, 16, {})
    assert result.rejected == [] and result.saved == 0
    async with fresh_sessionmaker() as session:
        runs = list(
            await session.scalars(
                select(models.Run).where(models.Run.experiment_id == result.experiment_id)
            )
        )
        spans = list(
            await session.scalars(
                select(models.Span).where(models.Span.run_id.in_([r.id for r in runs]))
            )
        )
        cases = {
            c.id: c.case_key
            for c in await session.scalars(
                select(models.DatasetCase).where(
                    models.DatasetCase.id.in_([r.case_id for r in runs])
                )
            )
        }
    assert len(runs) == 16 and all(r.input_tokens > 0 for r in runs)
    by_run: dict[str, list[models.Span]] = {}
    for span in spans:
        by_run.setdefault(span.run_id, []).append(span)
    for run in runs:
        tree = by_run[run.id]
        [root] = [s for s in tree if s.parent_span_id is None]
        assert (root.kind, root.name) == ("step", "handle_request")
        # Model and tool spans hang under the run's root span.
        assert all(s.parent_span_id == root.span_id for s in tree if s is not root)
        tools = [s.name for s in sorted(tree, key=lambda s: s.started_at) if s.kind == "tool"]
        assert any(s.kind == "model" for s in tree)
        if cases[run.case_id] in ("refund-returned", "refund-partial"):
            assert tools == ["get_order", "get_return", "issue_refund"]
        if cases[run.case_id] == "app-crash":
            assert tools == ["escalate"]
        if cases[run.case_id] == "gibberish":
            assert tools == []
