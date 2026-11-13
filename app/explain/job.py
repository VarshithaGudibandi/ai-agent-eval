import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func

from app.budget.pricing import Price, latest_price
from app.budget.reservations import BudgetExhaustedError, BudgetGuard, BudgetOwner
from app.db import models
from app.db.budgets import BudgetLimits, budget_for_report, create_budget
from app.db.ids import new_id
from app.db.jobs import LeaseLostError, enqueue, require_lease
from app.db.outcomes import main_version, run_failures
from app.db.scope import WorkspaceScope
from app.db.scores import scoring_input
from app.explain.auditor import MAX_RUNS, Audit, ShownRun, messages, read_reply
from app.explain.diff import ConfigChange, config_diff
from app.explain.groups import MAX_GROUPS, FailureGroup, group_failures
from app.explain.report import assemble
from app.metrics.comparisons import load_side
from app.providers.contract import GenerationSettings, ModelProvider
from app.providers.credentials import active_credential, decrypt_credential
from app.providers.key_vault import (
    KeyUnreadableError,
    MasterKeyMissingError,
    key_vault_from_settings,
)
from app.providers.metered import MeteredModel
from app.providers.rate_limiter import RateLimiter
from app.scoring.judge_job import judge_provider
from app.scoring.judge_prompts import JudgeReplyError, Transcript
from app.scoring.run_view import RunView

if TYPE_CHECKING:
    from app.jobs.worker import JobContext

logger = logging.getLogger(__name__)

EXPLAIN = "explain_report"
# One call per group, and one more try for a reply that is not the JSON asked for.
CALLS_PER_REPORT = MAX_GROUPS * 2


async def request_report(
    scope: WorkspaceScope, comparison_id: str, model: str
) -> models.RegressionReport:
    """Queue a report on why a comparison's candidate got worse, with a budget of its own."""
    await scope.require(models.Comparison, comparison_id)
    report = models.RegressionReport(
        id=new_id("rep"),
        workspace_id=scope.workspace_id,
        comparison_id=comparison_id,
        status=models.ReportStatus.QUEUED,
        model=model,
    )
    scope.add(report)
    await scope.flush()
    await create_budget(
        scope, None, BudgetLimits(requests=CALLS_PER_REPORT), regression_report_id=report.id
    )
    await enqueue(scope, EXPLAIN, report.id)
    return report


@dataclass(frozen=True)
class _Prepared:
    report: models.RegressionReport
    paired: bool
    changes: list[ConfigChange]
    groups: list[FailureGroup]
    shown: dict[str, list[ShownRun]]
    credential: models.ProviderCredential | None
    price: Price | None
    budget_id: str
    # Why no group can get a cause, decided before any call.
    refusal: str | None


async def _shown(scope: WorkspaceScope, run_ids: tuple[str, ...]) -> list[ShownRun]:
    shown = []
    for n, run_id in enumerate(run_ids[:MAX_RUNS], start=1):
        data = await scoring_input(scope, run_id)
        view = RunView.build(
            data.spans, data.run.tags, data.config, data.expected, data.has_expected
        )
        shown.append(ShownRun(f"r{n}", run_id, Transcript.of(view)))
    return shown


async def _prepare(ctx: "JobContext") -> _Prepared:
    claim = ctx.claim
    async with ctx.sessionmaker.begin() as session:
        await require_lease(session, claim.workspace_id, claim.job_id, claim.lease_token)
        scope = WorkspaceScope(session, claim.workspace_id)
        report = await scope.require(models.RegressionReport, claim.subject_id)
        comparison = await scope.require(models.Comparison, report.comparison_id)
        base, _, _ = await load_side(scope, comparison.baseline, "baseline")
        cand, _, _ = await load_side(scope, comparison.candidate, "candidate")
        paired = bool(comparison.result.get("paired"))
        worse = (
            {c["case_key"] for c in comparison.result.get("cases", []) if c["outcome"] == "worse"}
            if paired
            else None
        )
        base_ids = [o.run_id for o in base.outcomes]
        cand_ids = [o.run_id for o in cand.outcomes]
        groups = group_failures(
            await run_failures(scope, base_ids), await run_failures(scope, cand_ids), worse
        )
        base_version = await main_version(scope, base_ids)
        cand_version = await main_version(scope, cand_ids)
        changes = config_diff(
            base_version.config if base_version else {}, cand_version.config if cand_version else {}
        )
        shown = {g.key: await _shown(scope, g.run_ids) for g in groups}
        credential = await active_credential(scope, "gemini")
        refusal = None
        if credential is None:
            refusal = "no Gemini key in this workspace; add one to explain regressions"
        elif not paired and not credential.paid:
            refusal = (
                "a report on live runs needs a Gemini key marked paid: free-tier traffic may"
                " be used by the provider, and live traces are your customers' data"
            )
        row = await latest_price(scope, "gemini", report.model)
        budget = await budget_for_report(scope, report.id)
        return _Prepared(
            report,
            paired,
            changes,
            groups,
            shown,
            credential,
            None if row is None else Price.of(row),
            budget.id,
            refusal,
        )


async def _audit(
    ctx: "JobContext",
    prepared: _Prepared,
    group: FailureGroup,
    provider: ModelProvider,
    credential_id: str,
) -> Audit | str:
    claim, settings = ctx.claim, ctx.settings
    model = MeteredModel(
        provider,
        "gemini",
        price=prepared.price,
        guard=BudgetGuard(
            ctx.sessionmaker,
            claim.workspace_id,
            BudgetOwner("regression_report", prepared.report.id),
            prepared.budget_id,
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
        model=prepared.report.model, temperature=0, max_output_tokens=4096, json_output=True
    )
    runs = prepared.shown[group.key]
    asked = messages(prepared.changes, group, runs)
    try:
        for attempt in range(2):
            response = await model.generate(asked, [], generation)
            try:
                return read_reply(
                    response.text, runs, prepared.changes, prepared.paired, response.resolved_model
                )
            except JudgeReplyError:
                if attempt == 1:
                    raise
    except LeaseLostError:
        raise
    except BudgetExhaustedError:
        return "the report's budget is used up"
    except Exception as exc:
        logger.warning("an audit failed", extra={"group": group.key, "error": type(exc).__name__})
        return f"{type(exc).__name__}: {exc}"[:500]
    raise AssertionError("unreachable")


async def explain_job(ctx: "JobContext") -> None:
    """Write a report: the deterministic findings always, a cause per group when a model
    can be asked. Model calls happen outside any transaction; writes are fenced on the lease."""
    try:
        prepared = await _prepare(ctx)
    except LeaseLostError:
        raise
    except Exception as exc:
        # A report nobody can finish says so, instead of waiting forever as queued.
        await _fail(ctx, f"{type(exc).__name__}: {exc}"[:500])
        raise
    audits: dict[str, Audit | str] = {}
    if prepared.refusal is not None or prepared.credential is None:
        audits = {g.key: prepared.refusal or "" for g in prepared.groups}
    else:
        try:
            api_key = decrypt_credential(prepared.credential, key_vault_from_settings(ctx.settings))
        except (MasterKeyMissingError, KeyUnreadableError) as exc:
            audits = {g.key: str(exc)[:500] for g in prepared.groups}
        else:
            async with judge_provider(api_key, ctx.settings) as provider:
                for group in prepared.groups:
                    audits[group.key] = await _audit(
                        ctx, prepared, group, provider, prepared.credential.id
                    )
    report = assemble(prepared.changes, prepared.groups, audits, prepared.paired)
    claim = ctx.claim
    async with ctx.sessionmaker.begin() as session:
        await require_lease(session, claim.workspace_id, claim.job_id, claim.lease_token)
        scope = WorkspaceScope(session, claim.workspace_id)
        row = await scope.require(models.RegressionReport, prepared.report.id)
        row.report = report.model_dump(mode="json")
        row.status = models.ReportStatus.DONE
        row.finished_at = func.now()


async def _fail(ctx: "JobContext", error: str) -> None:
    claim = ctx.claim
    async with ctx.sessionmaker.begin() as session:
        await require_lease(session, claim.workspace_id, claim.job_id, claim.lease_token)
        row = await WorkspaceScope(session, claim.workspace_id).require(
            models.RegressionReport, claim.subject_id
        )
        row.status = models.ReportStatus.FAILED
        row.error = error
        row.finished_at = func.now()
