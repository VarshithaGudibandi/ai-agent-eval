import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.principal import Principal
from app.db import models
from app.db.budgets import BudgetLimits, budget_for_experiment, raise_limits
from app.db.datasets import CaseData, save_version
from app.db.experiments import start_experiment
from app.db.ids import new_id
from app.db.jobs import LeaseLostError, cancel, claim_next, finish
from app.db.reviews import NewLabel, add_review_item, save_labels
from app.db.scope import WorkspaceScope
from app.db.traces import agent_version
from app.ingest.redact import redact_value
from app.ingest.service import ingest
from app.jobs.worker import JobContext
from app.providers.credentials import create_credential
from app.providers.key_vault import key_vault_from_settings
from app.scoring import judge_job
from app.scoring.calibration import question_hash
from app.scoring.definitions import ScorerDefinition
from app.scoring.job import SCORE_RUN, score_run_job
from app.scoring.judge_job import JUDGE_RUN, judge_run_job
from app.scoring.specs import ensure_default_spec, save_definitions
from app.settings import Settings
from app.traces.normalize import fingerprint
from tests.scoring.golden import golden
from tests.scoring.scripted_judge import AnsweringJudge

Sessions = async_sessionmaker[AsyncSession]
FAKE_KEY = "AIza" + "J" * 35
QUESTIONS = [
    {"id": "looked_up", "text": "Did the agent look the order up before answering?"},
    {"id": "apologised", "text": "Does the reply apologise for the broken headphones?"},
]
QUOTES = {"looked_up": '"status": "delivered"', "apologised": "I refunded 59.0"}
SPEC = [
    ScorerDefinition(key="ran", check="run_completed"),
    ScorerDefinition(key="manners", check="checklist", params={"questions": QUESTIONS}),
]


@dataclass
class World:
    sessions: Sessions
    settings: Settings
    workspace_id: str
    experiment_id: str
    judge: AnsweringJudge

    def trace(self, repetition: int, experiment: bool = True) -> dict[str, Any]:
        trace = golden("refund")
        trace["trace_id"] = uuid.uuid4().hex
        if experiment:
            trace["tags"] = {
                "experiment": self.experiment_id,
                "case": "refund",
                "repetition": repetition,
            }
        return trace

    async def send(self, *traces: dict[str, Any]) -> list[str]:
        async with self.sessions.begin() as session:
            scope = WorkspaceScope(session, self.workspace_id)
            result = await ingest(scope, list(traces))
            assert result.rejected == [], result.rejected
            rows = await session.execute(
                select(models.Run.id)
                .join(models.Trace, models.Trace.id == models.Run.trace_row_id)
                .where(models.Trace.trace_id.in_([t["trace_id"] for t in traces]))
            )
        return [r[0] for r in rows]

    async def work(self, kind: str) -> int:
        """Run every waiting job of this kind, as the worker would."""
        handler = {SCORE_RUN: score_run_job, JUDGE_RUN: judge_run_job}[kind]
        done = 0
        while True:
            async with self.sessions.begin() as session:
                claim = await claim_next(session, "test", 30, [kind])
            if claim is None:
                return done
            await handler(JobContext(claim, self.sessions, self.settings))
            async with self.sessions.begin() as session:
                await finish(session, claim.workspace_id, claim.job_id, claim.lease_token)
            done += 1

    async def score(self, *traces: dict[str, Any]) -> list[str]:
        runs = await self.send(*traces)
        await self.work(SCORE_RUN)
        await self.work(JUDGE_RUN)
        return runs

    async def one(self, statement: Any) -> Any:
        async with self.sessions() as session:
            return (await session.execute(statement)).all()


@pytest.fixture
async def world(
    fresh_sessionmaker: Sessions, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> World:
    settings = settings.model_copy(update={"review_sample_share": 1.0})
    judge = AnsweringJudge({qid: ("yes", quote) for qid, quote in QUOTES.items()})

    @asynccontextmanager
    async def scripted(api_key: str, _: Settings) -> AsyncIterator[AnsweringJudge]:
        assert api_key == FAKE_KEY
        yield judge

    monkeypatch.setattr(judge_job, "judge_provider", scripted)
    async with fresh_sessionmaker.begin() as session:
        workspace = models.Workspace(id=new_id("ws"), name="judging")
        session.add(workspace)
        await session.flush()
        scope = WorkspaceScope(session, workspace.id)
        owner = Principal(
            actor_type=models.ActorType.USER,
            actor_id="usr_owner",
            user_id=None,
            workspace_id=workspace.id,
            role=models.Role.OWNER,
        )
        await create_credential(
            scope,
            owner,
            models.KeyedProvider.GEMINI,
            "judge",
            FAKE_KEY,
            key_vault_from_settings(settings),
        )
        dataset, _ = await save_version(scope, "refunds", [CaseData("refund", {"m": 1})])
        config = redact_value(golden("refund")["config"])
        version = await agent_version(scope, "support-agent", fingerprint(config), config, None)
        await ensure_default_spec(scope, version.agent_id)
        await save_definitions(scope, version.agent_id, SPEC)
        experiment, _ = await start_experiment(scope, version, dataset.id, 20, None)
    return World(fresh_sessionmaker, settings, workspace.id, experiment.id, judge)


def _verdicts(world: World) -> Any:
    return world.one(select(models.Verdict.status, models.Verdict.failed))


async def test_a_new_judge_is_asked_but_counts_only_once_trusted(world: World) -> None:
    [run_id] = await world.score(world.trace(1))
    answers = await world.one(
        select(
            models.JudgeAnswer.question_id,
            models.JudgeAnswer.answer,
            models.JudgeAnswer.quote,
            models.JudgeAnswer.trusted,
            models.JudgeAnswer.model_version,
        ).order_by(models.JudgeAnswer.question_id)
    )
    assert answers == [
        ("apologised", "yes", QUOTES["apologised"], False, "gemini-3.5-flash-lite-001"),
        ("looked_up", "yes", QUOTES["looked_up"], False, "gemini-3.5-flash-lite-001"),
    ]
    # No question is trusted yet, so the judge leaves no score and the rules decide.
    scores = await world.one(select(models.Scorer.key).join(models.Score))
    assert scores == [("ran",)]
    assert await _verdicts(world) == [("passed", [])]
    # Every call went through the meter and the experiment's budget.
    calls = await world.one(select(func.count()).select_from(models.ModelCall))
    assert calls == [(world.judge.calls,)] and world.judge.calls == 1
    review = await world.one(select(models.ReviewItem.run_id, models.ReviewItem.reason))
    assert review == [(run_id, "sampled")]


async def _label(world: World, runs: list[str], answers: Callable[[int], dict[str, str]]) -> None:
    async with world.sessions.begin() as session:
        scope = WorkspaceScope(session, world.workspace_id)
        scorer = await scope.first(scope.query(models.Scorer).where(models.Scorer.key == "manners"))
        assert scorer is not None
        for n, run_id in enumerate(runs):
            await add_review_item(scope, run_id, models.ReviewReason.MANUAL)
            item = await scope.first(
                scope.query(models.ReviewItem).where(models.ReviewItem.run_id == run_id)
            )
            assert item is not None
            text = {q["id"]: q["text"] for q in QUESTIONS}
            await save_labels(
                scope,
                item,
                [
                    NewLabel(scorer.id, qid, question_hash(text[qid]), answer)
                    for qid, answer in answers(n).items()
                ],
                "usr_reviewer",
                None,
            )


async def test_a_question_below_the_agreement_bar_stays_untrusted(world: World) -> None:
    runs = await world.score(*(world.trace(n) for n in range(1, 11)))
    # People agree with the judge on looked_up every time, on apologised only 6 times in 10.
    await _label(
        world, runs, lambda n: {"looked_up": "yes", "apologised": "yes" if n < 6 else "no"}
    )
    world.judge.answers = {
        "looked_up": ("yes", QUOTES["looked_up"]),
        "apologised": ("no", QUOTES["apologised"]),
    }
    [run_id] = await world.score(world.trace(11))
    trusted = await world.one(
        select(models.JudgeAnswer.question_id, models.JudgeAnswer.trusted).where(
            models.JudgeAnswer.run_id == run_id
        )
    )
    assert sorted(trusted) == [("apologised", False), ("looked_up", True)]
    # The untrusted "no" is not counted: the trusted question alone gives a full score.
    [(value, reason)] = await world.one(
        select(models.Score.value, models.Score.reason)
        .join(models.Scorer)
        .where(models.Score.run_id == run_id, models.Scorer.key == "manners")
    )
    assert (value, reason) == (1.0, "1 of 1 questions yes")


async def test_once_trusted_a_failing_answer_fails_the_run(world: World) -> None:
    runs = await world.score(*(world.trace(n) for n in range(1, 11)))
    await _label(world, runs, lambda n: {"looked_up": "yes", "apologised": "yes"})
    world.judge.answers = {
        "looked_up": ("yes", QUOTES["looked_up"]),
        "apologised": ("no", QUOTES["apologised"]),
    }
    calls_before = world.judge.calls
    [run_id] = await world.score(world.trace(11))
    [(status, failed)] = await world.one(
        select(models.Verdict.status, models.Verdict.failed).where(models.Verdict.run_id == run_id)
    )
    assert (status, failed) == ("failed", ["manners"])
    # Half the questions passed, close to the 0.7 bar, so the judge was asked three times.
    assert world.judge.calls - calls_before == 3
    [(confidence,)] = await world.one(
        select(models.Score.confidence)
        .join(models.Scorer)
        .where(models.Score.run_id == run_id, models.Scorer.key == "manners")
    )
    assert confidence == 1.0
    review = await world.one(
        select(models.ReviewItem.reason).where(models.ReviewItem.run_id == run_id)
    )
    assert review == [("borderline",)]


async def test_without_a_key_the_rules_still_score_and_the_verdict_is_incomplete(
    world: World,
) -> None:
    async with world.sessions.begin() as session:
        await session.execute(
            models.ProviderCredential.__table__.update().values(revoked_at=func.now())
        )
    await world.score(world.trace(1))
    assert await _verdicts(world) == [("incomplete", [])]
    [(errored,)] = await world.one(select(models.Verdict.errored))
    assert errored == ["manners"]
    rows = await world.one(
        select(models.Scorer.key, models.Score.passed, models.Score.error).join(models.Scorer)
    )
    assert sorted(rows) == [
        ("manners", None, "no Gemini key in this workspace; add one to judge runs"),
        ("ran", True, None),
    ]


async def test_a_used_up_budget_pauses_the_experiment_and_judges_nothing(world: World) -> None:
    async with world.sessions.begin() as session:
        scope = WorkspaceScope(session, world.workspace_id)
        budget = await budget_for_experiment(scope, world.experiment_id)
        await raise_limits(scope, budget, BudgetLimits(requests=0))
    await world.score(world.trace(1))
    assert world.judge.calls == 0
    assert await _verdicts(world) == [("incomplete", [])]
    [(status,)] = await world.one(select(models.Experiment.status))
    assert status == "paused_budget"


async def test_live_runs_are_graded_by_rules_alone(world: World) -> None:
    await world.score(world.trace(1, experiment=False))
    assert world.judge.calls == 0
    assert await _verdicts(world) == [("passed", [])]


async def test_a_worker_that_lost_its_lease_spends_nothing(world: World) -> None:
    [run_id] = await world.send(world.trace(1))
    await world.work(SCORE_RUN)
    async with world.sessions.begin() as session:
        claim = await claim_next(session, "stale", 30, [JUDGE_RUN])
    assert claim is not None
    async with world.sessions.begin() as session:
        await cancel(WorkspaceScope(session, world.workspace_id), run_id)
    with pytest.raises(LeaseLostError):
        await judge_run_job(JobContext(claim, world.sessions, world.settings))
    assert world.judge.calls == 0
    assert await world.one(select(func.count()).select_from(models.JudgeAnswer)) == [(0,)]
