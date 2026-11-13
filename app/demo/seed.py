"""Load the demo workspace, so a visitor can see a caught regression without a model key.

The traces are the demo agent's real recorded runs, v1 and v2 on the same 20 cases. They
go through the normal ingest, scoring, metrics and comparison, so every number on screen is
computed here. Only the regression report is stored: it needs a model, so it was written
once by gemini-3.5-flash when the data was recorded.

    python -m app.demo.seed
"""

import asyncio
import copy
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.users import DEMO_EMAIL_DOMAIN, create_user, find_user
from app.auth.workspaces import create_workspace, memberships_of
from app.db import models
from app.db.datasets import CaseData, save_version
from app.db.engine import create_engine, create_sessionmaker
from app.db.experiments import finish_experiment, start_experiment
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from app.db.traces import agent_version, run_ids_by_trace
from app.ingest.redact import redact_value
from app.ingest.service import ingest
from app.jobs.worker import run_queued
from app.metrics.comparisons import ExperimentSide, NewComparison, run_comparison
from app.scoring.definitions import EvalSpecDefinition
from app.scoring.specs import save_definitions
from app.settings import Settings, get_settings
from app.traces.normalize import fingerprint

DATA = Path(__file__).parent / "data"
WORKSPACE_NAME = "Demo shop"
OWNER_EMAIL = f"demo-owner@{DEMO_EMAIL_DOMAIN}"
VIEWER_EMAIL = f"demo@{DEMO_EMAIL_DOMAIN}"
REPORT_MODEL = "gemini-3.5-flash"
# Live traffic: two weeks of production runs, v1 until v2 shipped a few days ago.
LIVE_DAYS = 14
LIVE_PER_DAY = 12
V2_SHIPPED_DAYS_AGO = 3


def _recorded() -> dict[str, list[dict[str, Any]]]:
    by_version: dict[str, list[dict[str, Any]]] = {}
    for line in (DATA / "traces.jsonl").read_text().splitlines():
        row = json.loads(line)
        by_version.setdefault(row["version"], []).append(row["trace"])
    return by_version


def _moved(trace: dict[str, Any], trace_id: str, start: datetime) -> dict[str, Any]:
    """The trace as a production run with its own id, its spans shifted to begin at start."""
    moved = copy.deepcopy(trace)
    moved["trace_id"] = trace_id
    moved["tags"] = {"environment": "production"}
    first = min(datetime.fromisoformat(s["start"]) for s in moved["spans"])
    for span in moved["spans"]:
        for key in ("start", "end"):
            at = datetime.fromisoformat(span[key]) - first + start
            span[key] = at.isoformat()
    return moved


def live_traffic(recorded: dict[str, list[dict[str, Any]]], now: datetime) -> list[dict[str, Any]]:
    traces = []
    for days_ago in range(LIVE_DAYS, 0, -1):
        source = recorded["v2" if days_ago <= V2_SHIPPED_DAYS_AGO else "v1"]
        for n in range(LIVE_PER_DAY):
            start = now - timedelta(days=days_ago, hours=n)
            picked = source[(days_ago * LIVE_PER_DAY + n) % len(source)]
            traces.append(_moved(picked, f"live-{days_ago}-{n}", start))
    return traces


def _with_runs(report: Any, runs: dict[str, str]) -> Any:
    """The stored report, its citations pointed at this workspace's runs."""
    text = json.dumps(report)
    for trace_id, run_id in runs.items():
        text = text.replace(f'"trace:{trace_id}"', f'"{run_id}"')
    return json.loads(text)


async def _version(
    scope: WorkspaceScope, label: str, recorded: list[dict[str, Any]], dataset_id: str
) -> str:
    """Store one version's experiment and traces. Returns the experiment id."""
    config = redact_value(recorded[0]["config"])
    version = await agent_version(scope, recorded[0]["agent"], fingerprint(config), config, label)
    if label == "v1":
        spec = EvalSpecDefinition.model_validate_json((DATA / "scorers.json").read_text())
        await save_definitions(scope, version.agent_id, spec.scorers)
    experiment, _ = await start_experiment(scope, version, dataset_id, 2, None)
    tagged = [{**t, "tags": {**t["tags"], "experiment": experiment.id}} for t in recorded]
    await ingest(scope, tagged)
    await finish_experiment(scope, experiment.id)
    return experiment.id


async def _report(scope: WorkspaceScope, baseline: str, candidate: str) -> None:
    comparison, _ = await run_comparison(
        scope,
        NewComparison(
            baseline=ExperimentSide(experiment_id=baseline),
            candidate=ExperimentSide(experiment_id=candidate),
        ),
    )
    report = json.loads((DATA / "report.json").read_text())
    scope.add(
        models.RegressionReport(
            id=new_id("rep"),
            workspace_id=scope.workspace_id,
            comparison_id=comparison.id,
            status=models.ReportStatus.DONE,
            model=REPORT_MODEL,
            report=_with_runs(report, await run_ids_by_trace(scope)),
            finished_at=datetime.now(UTC),
        )
    )


async def demo_viewer(session: AsyncSession) -> models.User | None:
    """The account the demo login signs in as, once the demo is loaded."""
    return await find_user(session, VIEWER_EMAIL)


class DemoIncompleteError(RuntimeError):
    """An earlier load stopped part way."""


async def seed(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    now: datetime | None = None,
) -> str:
    """Create the demo workspace once and return its id. Running it again changes nothing."""
    now = now or datetime.now(UTC)
    recorded = _recorded()
    async with sessionmaker.begin() as session:
        viewer = await demo_viewer(session)
        if viewer is not None:
            [(_, workspace)] = await memberships_of(session, viewer.id)
            return workspace.id
        if await find_user(session, OWNER_EMAIL) is not None:
            raise DemoIncompleteError(
                "an earlier demo load stopped part way; start from an empty database"
            )
        # Nobody signs in as the owner: it exists because every workspace has one.
        owner = await create_user(session, OWNER_EMAIL, secrets.token_urlsafe(32))
        workspace = await create_workspace(session, owner, WORKSPACE_NAME)
        cases = json.loads((DATA / "cases.json").read_text())
        dataset, _ = await save_version(
            WorkspaceScope(session, workspace.id), "support cases", [CaseData(**c) for c in cases]
        )
    experiments = {}
    # One transaction each, so v2 is stored after v1 and the pages see it as the newer one.
    for label in ("v1", "v2"):
        async with sessionmaker.begin() as session:
            scope = WorkspaceScope(session, workspace.id)
            experiments[label] = await _version(scope, label, recorded[label], dataset.id)
    async with sessionmaker.begin() as session:
        await ingest(WorkspaceScope(session, workspace.id), live_traffic(recorded, now))
    await run_queued(sessionmaker, settings, workspace.id)
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace.id)
        await _report(scope, experiments["v1"], experiments["v2"])
        # The viewer comes last: its existence is what says the load finished. The demo login
        # starts a session for it without a password.
        viewer = await create_user(session, VIEWER_EMAIL, secrets.token_urlsafe(32))
        scope.add(
            models.Membership(workspace_id=workspace.id, user_id=viewer.id, role=models.Role.VIEWER)
        )
    return workspace.id


async def _main() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        workspace_id = await seed(create_sessionmaker(engine), settings)
    finally:
        await engine.dispose()
    print(f"Demo workspace ready: {workspace_id}")


if __name__ == "__main__":
    asyncio.run(_main())
