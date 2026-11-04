from typing import Any

from app.db import models
from app.db.ids import new_id
from app.db.reports import RunFacts, run_facts
from app.db.scope import WorkspaceScope
from app.reports.comparison import Side


class SideUnclearError(ValueError):
    """The experiment does not run that config, or runs several and none was named."""


async def load_side(
    scope: WorkspaceScope, experiment_id: str, agent_config_id: str | None
) -> tuple[Side, list[RunFacts]]:
    experiment = await scope.require(models.Experiment, experiment_id)
    configs = [c["id"] for c in experiment.manifest["agent_configs"]]
    if agent_config_id is None:
        if len(configs) != 1:
            raise SideUnclearError(
                f"experiment {experiment.id} runs {len(configs)} configs; name one"
            )
        agent_config_id = configs[0]
    elif agent_config_id not in configs:
        raise SideUnclearError(f"experiment {experiment.id} does not run {agent_config_id}")
    config = await scope.require(models.AgentConfig, agent_config_id)
    side = Side(
        experiment_id=experiment.id,
        agent_config_id=config.id,
        config_name=config.name,
        mode=experiment.mode,
        status=experiment.status,
        dataset_version_id=experiment.dataset_version_id,
        split=experiment.split,
        environments=experiment.manifest["environments"],
        evaluators=experiment.manifest["evaluators"],
        limits=config.limits,
    )
    facts = [f for f in await run_facts(scope, experiment.id) if f.agent_config_id == config.id]
    return side, facts


def save_comparison(
    scope: WorkspaceScope, baseline: Side, candidate: Side, result: dict[str, Any]
) -> models.Comparison:
    row = models.Comparison(
        id=new_id("cmp"),
        workspace_id=scope.workspace_id,
        baseline_experiment_id=baseline.experiment_id,
        baseline_config_id=baseline.agent_config_id,
        candidate_experiment_id=candidate.experiment_id,
        candidate_config_id=candidate.agent_config_id,
        result=result,
    )
    scope.add(row)
    return row


async def list_comparisons(scope: WorkspaceScope) -> list[models.Comparison]:
    return await scope.all(
        scope.query(models.Comparison).order_by(
            models.Comparison.created_at.desc(), models.Comparison.id
        )
    )
