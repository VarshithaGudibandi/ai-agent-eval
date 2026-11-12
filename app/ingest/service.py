import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.budget.pricing import Price, latest_price
from app.db import models
from app.db.experiments import case_by_key, has_run
from app.db.rate_limits import take_ingest
from app.db.scope import WorkspaceScope
from app.db.traces import ExperimentLink, agent_version, save_run, store_raw, trace_known
from app.ingest.redact import redact_value
from app.ingest.trace_format import Trace
from app.traces.normalize import fingerprint, normalize

logger = logging.getLogger(__name__)


class BatchTooLargeError(Exception):
    """The request body is over the ingest size limit."""


class IngestRateLimitedError(Exception):
    """The workspace sent more traces this minute than it may."""

    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(f"too many traces; try again in {retry_after_seconds:.0f} seconds")
        self.retry_after_seconds = retry_after_seconds


class BatchInvalidError(Exception):
    """The request is not a batch of traces at all, so nothing in it can be judged."""


@dataclass(frozen=True)
class Rejected:
    trace_id: str | None
    error: str


@dataclass
class IngestResult:
    accepted: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)


def _problem(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:3]:
        where = ".".join(str(p) for p in error["loc"])
        parts.append(f"{where}: {error['msg']}" if where else error["msg"])
    return "; ".join(parts)


async def _prices(
    scope: WorkspaceScope, trace: Trace, known: dict[tuple[str, str], Price | None]
) -> dict[tuple[str, str], Price]:
    for span in trace.spans:
        if span.kind != "model" or span.provider is None or span.model is None:
            continue
        key = (span.provider, span.model)
        if key not in known:
            row = await latest_price(scope, *key)
            known[key] = None if row is None else Price.of(row)
    return {key: price for key, price in known.items() if price is not None}


class LinkError(ValueError):
    """A trace claims a place in an experiment that it cannot have."""


async def _link(scope: WorkspaceScope, trace: Trace) -> ExperimentLink | None:
    """Check the experiment tags of a build-loop trace. Live traces carry none."""
    names = ("experiment", "case", "repetition")
    given = {n: trace.tags.get(n) for n in names}
    if all(v is None for v in given.values()):
        return None
    if any(v is None for v in given.values()):
        raise LinkError("a trace in an experiment needs experiment, case and repetition tags")
    experiment = await scope.get(models.Experiment, str(given["experiment"]))
    if experiment is None:
        raise LinkError(f"no experiment {given['experiment']} in this workspace")
    repetition = given["repetition"]
    if not isinstance(repetition, int) or not 1 <= repetition <= experiment.repetitions:
        raise LinkError(f"repetition must be a whole number from 1 to {experiment.repetitions}")
    case = await case_by_key(scope, experiment, str(given["case"]))
    if case is None:
        raise LinkError(f"case {given['case']} is not in the experiment's dataset")
    # A run counts toward the experiment only if it is the version the experiment is about.
    version = await scope.require(models.AgentVersion, experiment.agent_version_id)
    agent = await scope.require(models.Agent, version.agent_id)
    if (trace.agent, fingerprint(trace.config)) != (agent.name, version.fingerprint):
        raise LinkError("the trace's agent or config differs from the experiment's")
    if await has_run(scope, experiment.id, case.id, repetition):
        raise LinkError(f"repetition {repetition} of case {case.case_key} already has a run")
    return ExperimentLink(experiment.id, case.id, repetition)


async def admit(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, traces: int, per_minute: int
) -> None:
    """Refuse the batch if the workspace is over its ingest rate.

    A short transaction of its own, so the bucket's row lock is not held while the batch is
    stored, which would make one workspace's concurrent sends wait on each other.
    """
    async with sessionmaker.begin() as session:
        wait = await take_ingest(WorkspaceScope(session, workspace_id), traces, per_minute)
    if wait > 0:
        raise IngestRateLimitedError(wait)


async def ingest(scope: WorkspaceScope, documents: list[dict[str, Any]]) -> IngestResult:
    """Store each trace as received, then build its run, spans, cost and agent version.

    Each trace stands alone: one that fails validation is rejected with the reason, and the
    rest of the batch still goes in. A trace already stored is reported as a duplicate, so
    the SDK can resend a batch safely.
    """
    result = IngestResult()
    known_prices: dict[tuple[str, str], Price | None] = {}
    for document in documents:
        # Masking comes first, so a secret can reach neither storage nor an error message.
        masked = redact_value(document)
        claimed_id = masked.get("trace_id") if isinstance(masked, dict) else None
        try:
            trace = Trace.model_validate(masked)
        except ValidationError as exc:
            result.rejected.append(
                Rejected(claimed_id if isinstance(claimed_id, str) else None, _problem(exc))
            )
            continue
        # A resent trace is a duplicate before anything else, even if its run slot is taken.
        if await trace_known(scope, trace.trace_id):
            result.duplicates.append(trace.trace_id)
            continue
        try:
            link = await _link(scope, trace)
        except LinkError as exc:
            result.rejected.append(Rejected(trace.trace_id, str(exc)))
            continue
        row = await store_raw(scope, trace.trace_id, trace.agent, trace.model_dump(mode="json"))
        if row is None:
            result.duplicates.append(trace.trace_id)
            continue
        result.accepted.append(trace.trace_id)
        try:
            # A savepoint: if building the run fails, the raw trace is still kept.
            async with scope.session.begin_nested():
                version = await agent_version(
                    scope, trace.agent, fingerprint(trace.config), trace.config, trace.label
                )
                run = normalize(trace, await _prices(scope, trace, known_prices))
                await save_run(scope, row, version, trace.tags, run, link)
        except Exception as exc:
            logger.exception("could not normalize a trace", extra={"trace_id": trace.trace_id})
            row.status = models.TraceStatus.FAILED
            row.error = f"{type(exc).__name__}: {exc}"[:500]
    return result
