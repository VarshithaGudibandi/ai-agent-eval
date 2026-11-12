import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from app.budget.pricing import Price, latest_price
from app.budget.reservations import BudgetExhaustedError, BudgetGuard
from app.db import models
from app.db.jobs import LeaseLostError, enqueue, require_lease
from app.db.reviews import NewJudgeAnswer, add_review_item, labelled_answers, save_judge_answers
from app.db.scope import WorkspaceScope
from app.db.scores import NewScore, save_scores, save_verdict, scores_by, scoring_input
from app.providers.contract import GenerationSettings, Message, ModelProvider, ModelResponse
from app.providers.credentials import active_credential, decrypt_credential
from app.providers.gemini import GeminiProvider
from app.providers.key_vault import (
    KeyUnreadableError,
    MasterKeyMissingError,
    key_vault_from_settings,
)
from app.providers.metered import MeteredModel
from app.providers.rate_limiter import RateLimiter
from app.scoring.calibration import calibrate, question_hash
from app.scoring.engine import Scorer, ScoreResult, applies, verdict
from app.scoring.judge_access import JudgeAccess, judge_access
from app.scoring.judges import (
    JUDGES,
    ChecklistParams,
    ClaimCheckParams,
    JudgeResult,
    outcome,
    questions_of,
    run_checklist,
    run_claim_check,
)
from app.scoring.run_view import RunView
from app.scoring.specs import matching_labels, scorers_for
from app.settings import Settings

if TYPE_CHECKING:
    from app.jobs.worker import JobContext

logger = logging.getLogger(__name__)

JUDGE_RUN = "judge_run"


class JudgeUnavailableError(Exception):
    """The workspace cannot judge right now, for example because it has no Gemini key."""


def judges_for(run: RunView, scorers: list[Scorer]) -> list[Scorer]:
    return [s for s in scorers if s.definition.is_judge and applies(s.definition.applies_when, run)]


async def queue_judging(
    scope: WorkspaceScope, run: models.Run, view: RunView, scorers: list[Scorer], spec_id: str
) -> bool:
    """Queue the run's judges, if any apply and the run may be judged. True if queued, in
    which case the verdict waits for them."""
    if not judges_for(view, scorers) or await judge_access(scope, run) is None:
        return False
    await enqueue(scope, JUDGE_RUN, run.id, {"eval_spec_id": spec_id})
    return True


@asynccontextmanager
async def judge_provider(api_key: str, settings: Settings) -> AsyncIterator[ModelProvider]:
    async with httpx.AsyncClient() as client:
        yield GeminiProvider(api_key, client, settings.gemini_base_url)


@dataclass(frozen=True)
class _Prepared:
    view: RunView
    run: models.Run
    agent_id: str
    access: JudgeAccess
    scorers: list[Scorer]
    judges: list[Scorer]
    # Trusted question ids, per judge scorer.
    trusted: dict[str, set[str]]
    credential: models.ProviderCredential | None
    prices: dict[str, Price | None]


async def _prepare(ctx: "JobContext", spec_id: str) -> _Prepared | None:
    claim = ctx.claim
    async with ctx.sessionmaker.begin() as session:
        await require_lease(session, claim.workspace_id, claim.job_id, claim.lease_token)
        scope = WorkspaceScope(session, claim.workspace_id)
        data = await scoring_input(scope, claim.subject_id)
        access = await judge_access(scope, data.run)
        if access is None:
            return None
        scorers = await scorers_for(scope, spec_id)
        view = RunView.build(
            data.spans, data.run.tags, data.config, data.expected, data.has_expected
        )
        judges = judges_for(view, scorers)
        labelled = await labelled_answers(scope, data.agent_id)
        trusted: dict[str, set[str]] = {}
        prices: dict[str, Price | None] = {}
        for judge in judges:
            params = JUDGES[judge.definition.check].model_validate(judge.definition.params)
            trusted[judge.id] = {
                q.id
                for q in questions_of(params)
                if calibrate(
                    (a.judge, a.human)
                    for a in matching_labels(labelled, judge.definition.key, q.id, q.text)
                ).trusted
            }
            if params.model not in prices:
                row = await latest_price(scope, "gemini", params.model)
                prices[params.model] = None if row is None else Price.of(row)
        credential = await active_credential(scope, "gemini")
        return _Prepared(
            view, data.run, data.agent_id, access, scorers, judges, trusted, credential, prices
        )


async def _judge(
    ctx: "JobContext",
    prepared: _Prepared,
    judge: Scorer,
    provider: ModelProvider,
    credential_id: str,
) -> JudgeResult:
    claim, settings = ctx.claim, ctx.settings
    params = JUDGES[judge.definition.check].model_validate(judge.definition.params)
    model = MeteredModel(
        provider,
        "gemini",
        price=prepared.prices[params.model],
        guard=BudgetGuard(
            ctx.sessionmaker,
            claim.workspace_id,
            prepared.access.experiment_id,
            prepared.access.budget_id,
            claim.job_id,
            claim.lease_token,
        ),
        limiter=RateLimiter(ctx.sessionmaker, claim.workspace_id, credential_id),
        sessionmaker=ctx.sessionmaker,
        workspace_id=claim.workspace_id,
        timeout_seconds=settings.model_call_timeout_seconds,
        max_retries=settings.model_call_max_retries,
    )
    generation = GenerationSettings(
        model=params.model, temperature=0, max_output_tokens=2048, json_output=True
    )

    async def ask(messages: list[Message]) -> ModelResponse:
        return await model.generate(messages, [], generation)

    if isinstance(params, ChecklistParams):
        return await run_checklist(prepared.view, params, judge.definition.pass_mark, ask)
    if isinstance(params, ClaimCheckParams):
        return await run_claim_check(prepared.view, params, ask)
    raise TypeError(judge.definition.check)


async def _judge_or_reason(
    ctx: "JobContext",
    prepared: _Prepared,
    judge: Scorer,
    provider: ModelProvider,
    credential_id: str,
) -> JudgeResult | str:
    """The judge's result, or why it has none. Only a lost lease stops the job."""
    try:
        return await _judge(ctx, prepared, judge, provider, credential_id)
    except LeaseLostError:
        raise
    except BudgetExhaustedError:
        return "the experiment's budget is used up"
    except Exception as exc:
        # Like a crashing rule, a failing judge records an error, never a pass or a fail.
        logger.warning(
            "a judge failed", extra={"scorer": judge.definition.key, "error": type(exc).__name__}
        )
        return f"{type(exc).__name__}: {exc}"[:500]


def _sampled(run_id: str, share: float) -> bool:
    # From the run id, not a random draw, so judging a run again never changes the pick.
    point = int(hashlib.sha256(run_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return point < share


async def judge_run_job(ctx: "JobContext") -> None:
    """Run a scored run's judges, then store their answers, scores and the run's verdict.

    Model calls happen outside any transaction; every write is fenced on the lease. A judge
    that cannot answer leaves a scoring error, so the verdict is incomplete, never invented.
    """
    claim = ctx.claim
    spec_id = str(claim.payload["eval_spec_id"])
    prepared = await _prepare(ctx, spec_id)
    if prepared is None:
        return
    results: dict[str, JudgeResult | str] = {}
    try:
        if prepared.credential is None:
            raise JudgeUnavailableError("no Gemini key in this workspace; add one to judge runs")
        credential_id = prepared.credential.id
        api_key = decrypt_credential(prepared.credential, key_vault_from_settings(ctx.settings))
    except (JudgeUnavailableError, MasterKeyMissingError, KeyUnreadableError) as exc:
        results = {judge.id: str(exc)[:500] for judge in prepared.judges}
    else:
        async with judge_provider(api_key, ctx.settings) as provider:
            for judge in prepared.judges:
                results[judge.id] = await _judge_or_reason(
                    ctx, prepared, judge, provider, credential_id
                )
    await _store(ctx, prepared, spec_id, results)


async def _store(
    ctx: "JobContext", prepared: _Prepared, spec_id: str, results: dict[str, JudgeResult | str]
) -> None:
    claim = ctx.claim
    answers: list[NewJudgeAnswer] = []
    scores: list[NewScore] = []
    borderline = False
    for judge in prepared.judges:
        result = results[judge.id]
        if isinstance(result, str):
            scores.append(NewScore(judge.id, None, None, None, "", [], result))
            continue
        borderline = borderline or result.borderline
        trusted = prepared.trusted[judge.id]
        claims = [vars(c) | {"evidence": list(c.evidence)} for c in result.claims or []]
        for a in result.answers:
            answers.append(
                NewJudgeAnswer(
                    judge.id,
                    a.question.id,
                    a.question.text,
                    question_hash(a.question.text),
                    a.answer,
                    a.quote,
                    list(a.evidence),
                    a.confidence,
                    a.question.id in trusted,
                    result.model_version,
                    {"claims": claims} if result.claims is not None else {},
                )
            )
        scored = outcome(result, trusted)
        if scored is not None:
            scores.append(
                NewScore(
                    judge.id,
                    None,
                    scored.value,
                    scored.value >= judge.definition.pass_mark,
                    scored.reason,
                    list(scored.evidence),
                    None,
                    scored.confidence,
                )
            )
    async with ctx.sessionmaker.begin() as session:
        await require_lease(session, claim.workspace_id, claim.job_id, claim.lease_token)
        scope = WorkspaceScope(session, claim.workspace_id)
        run_id = prepared.run.id
        await save_judge_answers(scope, run_id, answers)
        await save_scores(scope, run_id, scores)
        stored = await scores_by(scope, run_id, [s.id for s in prepared.scorers])
        keys = {s.id: s.definition.key for s in prepared.scorers}
        final = verdict(
            prepared.scorers,
            [
                ScoreResult(
                    s.scorer_id,
                    keys[s.scorer_id],
                    s.span_id,
                    s.value,
                    s.passed,
                    s.reason,
                    tuple(s.evidence),
                    s.error,
                    s.confidence,
                )
                for s in stored
            ],
        )
        await save_verdict(
            scope,
            run_id,
            spec_id,
            final.status,
            final.quality,
            list(final.failed),
            list(final.errored),
        )
        if answers and (borderline or _sampled(run_id, ctx.settings.review_sample_share)):
            reason = models.ReviewReason.BORDERLINE if borderline else models.ReviewReason.SAMPLED
            await add_review_item(scope, run_id, reason)
