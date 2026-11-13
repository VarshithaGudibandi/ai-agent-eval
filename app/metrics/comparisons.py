from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from app.db import models
from app.db.ids import new_id
from app.db.outcomes import Loaded, experiment_outcomes, live_outcomes
from app.db.scope import NotFoundError, WorkspaceScope
from app.metrics.comparison import ComparisonResult, Gates, SideFacts, compare, seed_for


class ExperimentSide(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experiment_id: str


class WindowSide(BaseModel):
    """Live runs of an agent, or of one version of it, that started in [start, end)."""

    model_config = ConfigDict(extra="forbid")
    agent_id: str
    version_id: str | None = None
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _ordered(self) -> "WindowSide":
        if self.end <= self.start:
            raise ValueError("a window must end after it starts")
        return self


class NewComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline: ExperimentSide | WindowSide
    candidate: ExperimentSide | WindowSide
    # Leave out to use the agent's stored gates.
    gates: Gates | None = None

    @model_validator(mode="after")
    def _same_kind(self) -> "NewComparison":
        if type(self.baseline) is not type(self.candidate):
            raise ValueError("compare two experiments or two live windows, not one of each")
        return self


async def _experiment_side(
    scope: WorkspaceScope, side: ExperimentSide, label: str
) -> tuple[Loaded, SideFacts, str]:
    experiment = await scope.require(models.Experiment, side.experiment_id)
    version = await scope.require(models.AgentVersion, experiment.agent_version_id)
    loaded = await experiment_outcomes(scope, experiment.id)
    facts = SideFacts(
        label=label,
        dataset_version_id=experiment.dataset_version_id,
        eval_spec_ids=loaded.spec_ids,
        finished=experiment.status == models.ExperimentStatus.FINISHED,
        repetitions=experiment.repetitions,
    )
    return loaded, facts, version.agent_id


async def _window_side(
    scope: WorkspaceScope, side: WindowSide, label: str
) -> tuple[Loaded, SideFacts, str]:
    await scope.require(models.Agent, side.agent_id)
    if side.version_id is not None:
        version = await scope.require(models.AgentVersion, side.version_id)
        if version.agent_id != side.agent_id:
            raise NotFoundError(f"agent_versions:{side.version_id}")
    loaded = await live_outcomes(scope, side.agent_id, side.version_id, side.start, side.end)
    return loaded, SideFacts(label, None, loaded.spec_ids, True, None), side.agent_id


async def stored_gates(scope: WorkspaceScope, agent_id: str) -> Gates:
    agent = await scope.require(models.Agent, agent_id)
    return Gates.model_validate(agent.gates)


async def run_comparison(
    scope: WorkspaceScope, request: NewComparison
) -> tuple[models.Comparison, ComparisonResult]:
    """Compare two sides and keep the result, so CI and later the explainer can cite it."""
    sides = []
    for side, label in ((request.baseline, "baseline"), (request.candidate, "candidate")):
        if isinstance(side, ExperimentSide):
            sides.append(await _experiment_side(scope, side, label))
        else:
            sides.append(await _window_side(scope, side, label))
    (base, base_facts, agent_id), (cand, cand_facts, _) = sides
    gates = request.gates or await stored_gates(scope, agent_id)
    paired = isinstance(request.baseline, ExperimentSide)
    seed = seed_for(request.baseline.model_dump_json(), request.candidate.model_dump_json())
    result = compare(base.outcomes, cand.outcomes, base_facts, cand_facts, gates, paired, seed)
    row = models.Comparison(
        id=new_id("cmp"),
        workspace_id=scope.workspace_id,
        baseline=request.baseline.model_dump(mode="json"),
        candidate=request.candidate.model_dump(mode="json"),
        gates=gates.model_dump(mode="json"),
        decision=result.decision,
        result=result.model_dump(mode="json"),
    )
    scope.add(row)
    await scope.flush()
    return row, result
