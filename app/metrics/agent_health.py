from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from app.db import models
from app.db.experiments import experiments_of
from app.db.outcomes import (
    Loaded,
    baseline_experiment,
    experiment_outcomes,
    live_outcomes,
    run_failures,
)
from app.db.scope import NotFoundError, WorkspaceScope
from app.explain.groups import FailureGroup, group_failures
from app.metrics.comparison import Gates, MetricChange, SideFacts, compare, seed_for
from app.metrics.health import Health, health

# How an agent is doing right now, for its overview page and later the MCP server. An agent
# with experiments is judged by its newest one against that one's baseline; an agent with
# only live traffic by its last week against the week before.

WINDOW = timedelta(days=7)

Mode = Literal["none", "single", "versions", "live"]


class SideOut(BaseModel):
    agent_version_id: str | None
    experiment_id: str | None
    # For live windows: runs that started in [start, end).
    start: datetime | None
    end: datetime | None
    finished: bool
    health: Health


class AgentHealth(BaseModel):
    # none: no runs yet. single: one side only, nothing to compare with. versions: the
    # newest experiment against its baseline. live: the last week against the week before.
    mode: Mode
    current: SideOut | None
    baseline: SideOut | None
    metrics: list[MetricChange]
    fairness: list[str]


def _experiment_side(row: models.Experiment, loaded: Loaded) -> SideOut:
    return SideOut(
        agent_version_id=row.agent_version_id,
        experiment_id=row.id,
        start=None,
        end=None,
        finished=row.status == models.ExperimentStatus.FINISHED,
        health=health(loaded.outcomes),
    )


def _window_side(loaded: Loaded, start: datetime, end: datetime) -> SideOut:
    return SideOut(
        agent_version_id=None,
        experiment_id=None,
        start=start,
        end=end,
        finished=True,
        health=health(loaded.outcomes),
    )


async def live_windows(
    scope: WorkspaceScope, agent_id: str, end: datetime
) -> tuple[Loaded, Loaded]:
    """The week before end, and the week before that."""
    start = end - WINDOW
    current = await live_outcomes(scope, agent_id, None, start, end)
    previous = await live_outcomes(scope, agent_id, None, start - WINDOW, start)
    return previous, current


def live_facts(loaded: Loaded, label: str) -> SideFacts:
    return SideFacts(label, None, loaded.spec_ids, True, None)


async def agent_health(
    scope: WorkspaceScope, agent_id: str, gates: Gates, end: datetime
) -> AgentHealth:
    found = await experiments_of(scope, agent_id, 1)
    if found:
        newest = found[0]
        current = await experiment_outcomes(scope, newest.id)
        base_row = await baseline_experiment(scope, newest)
        if base_row is None:
            return AgentHealth(
                mode="single",
                current=_experiment_side(newest, current),
                baseline=None,
                metrics=[],
                fairness=[],
            )
        base = await experiment_outcomes(scope, base_row.id)
        result = compare(
            base.outcomes,
            current.outcomes,
            SideFacts(
                "baseline",
                base_row.dataset_version_id,
                base.spec_ids,
                base_row.status == models.ExperimentStatus.FINISHED,
                base_row.repetitions,
            ),
            SideFacts(
                "candidate",
                newest.dataset_version_id,
                current.spec_ids,
                newest.status == models.ExperimentStatus.FINISHED,
                newest.repetitions,
            ),
            gates,
            paired=True,
            seed=seed_for(base_row.id, newest.id),
        )
        return AgentHealth(
            mode="versions",
            current=_experiment_side(newest, current),
            baseline=_experiment_side(base_row, base),
            metrics=result.metrics,
            fairness=result.fairness,
        )
    previous, current = await live_windows(scope, agent_id, end)
    start = end - WINDOW
    if not current.outcomes and not previous.outcomes:
        return AgentHealth(mode="none", current=None, baseline=None, metrics=[], fairness=[])
    if not previous.outcomes:
        return AgentHealth(
            mode="single",
            current=_window_side(current, start, end),
            baseline=None,
            metrics=[],
            fairness=[],
        )
    result = compare(
        previous.outcomes,
        current.outcomes,
        live_facts(previous, "previous week"),
        live_facts(current, "last week"),
        gates,
        paired=False,
        seed=0,
        bootstrap=False,
    )
    return AgentHealth(
        mode="live",
        current=_window_side(current, start, end),
        baseline=_window_side(previous, start - WINDOW, start),
        metrics=result.metrics,
        fairness=result.fairness,
    )


async def current_runs(
    scope: WorkspaceScope, agent_id: str, experiment_id: str | None, end: datetime
) -> Loaded:
    """The runs the overview is about: the named experiment, else the newest one, else the
    last week of live runs."""
    if experiment_id is None:
        found = await experiments_of(scope, agent_id, 1)
        experiment_id = found[0].id if found else None
    if experiment_id is None:
        return await live_outcomes(scope, agent_id, None, end - WINDOW, end)
    experiment = await scope.require(models.Experiment, experiment_id)
    version = await scope.require(models.AgentVersion, experiment.agent_version_id)
    if version.agent_id != agent_id:
        raise NotFoundError(f"experiments:{experiment_id}")
    return await experiment_outcomes(scope, experiment_id)


async def failing_ways(scope: WorkspaceScope, loaded: Loaded) -> list[FailureGroup]:
    """The commonest ways these runs failed, by scorer and by judge question."""
    failures = await run_failures(scope, [o.run_id for o in loaded.outcomes])
    return group_failures([], failures, None)
