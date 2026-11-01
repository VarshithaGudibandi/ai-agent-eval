from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.ids import new_id


@dataclass
class Chain:
    workspace_id: str
    dataset_version_id: str
    task_version_id: str
    agent_config_id: str
    experiment_id: str
    run_id: str
    attempt_id: str


def _experiment(
    workspace_id: str, dataset_version_id: str, **overrides: object
) -> models.Experiment:
    values: dict[str, object] = {
        "id": new_id("exp"),
        "workspace_id": workspace_id,
        "dataset_version_id": dataset_version_id,
        "mode": "real",
        "split": "dev",
        "final": False,
        "status": "queued",
        "repetitions": 1,
        "manifest": {},
    }
    values.update(overrides)
    return models.Experiment(**values)


def _run(chain: Chain, **overrides: object) -> models.Run:
    values: dict[str, object] = {
        "id": new_id("run"),
        "workspace_id": chain.workspace_id,
        "experiment_id": chain.experiment_id,
        "agent_config_id": chain.agent_config_id,
        "task_version_id": chain.task_version_id,
        "repetition": 1,
        "execution_status": "queued",
        "evaluation_status": "pending",
    }
    values.update(overrides)
    return models.Run(**values)


def _attempt(chain: Chain, number: int) -> models.Attempt:
    return models.Attempt(
        id=new_id("att"),
        workspace_id=chain.workspace_id,
        run_id=chain.run_id,
        attempt_number=number,
        env_id=new_id("env"),
        execution_status="queued",
    )


def _event(chain: Chain, sequence: int) -> models.TraceEvent:
    return models.TraceEvent(
        id=new_id("evt"),
        workspace_id=chain.workspace_id,
        attempt_id=chain.attempt_id,
        sequence_number=sequence,
        event_type="note",
        payload={},
        occurred_at=datetime.now(UTC),
        elapsed_ms=0,
    )


def _score(chain: Chain) -> models.Score:
    return models.Score(
        id=new_id("score"),
        workspace_id=chain.workspace_id,
        attempt_id=chain.attempt_id,
        evaluator_name="refund_outcome",
        evaluator_version="1.0.0",
        layer="outcome",
        dimension="refund_amount",
        applicable=True,
        status="pass",
        score=1.0,
        evidence={},
    )


async def _chain(session: AsyncSession, workspace_id: str) -> Chain:
    dataset = models.Dataset(id=new_id("ds"), workspace_id=workspace_id, name=new_id("name"))
    version = models.DatasetVersion(
        id=new_id("dsv"),
        workspace_id=workspace_id,
        dataset_id=dataset.id,
        version=1,
        content_hash="h",
    )
    task = models.TaskVersion(
        id=new_id("task"),
        workspace_id=workspace_id,
        dataset_version_id=version.id,
        task_key="t1",
        environment="support-retail@1",
        scenario_family="refund",
        split="dev",
        agent_input={},
        fixture={},
        evaluator_spec={},
        limits={},
        category="c",
        difficulty="easy",
        review={},
        content_hash="h",
    )
    config = models.AgentConfig(
        id=new_id("cfg"),
        workspace_id=workspace_id,
        name="c",
        adapter_type="scripted",
        settings={},
        limits={},
        config_hash="h",
    )
    experiment = _experiment(workspace_id, version.id)
    session.add_all([dataset, version, task, config, experiment])
    await session.flush()
    chain = Chain(workspace_id, version.id, task.id, config.id, experiment.id, "", "")
    run = _run(chain)
    session.add(run)
    await session.flush()
    chain.run_id = run.id
    attempt = _attempt(chain, 1)
    session.add(attempt)
    await session.flush()
    chain.attempt_id = attempt.id
    return chain


async def _rejected(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, *rows: object
) -> None:
    with pytest.raises(IntegrityError):
        async with sessionmaker.begin() as session:
            chain = await _chain(session, workspace_id)
            for build in rows:
                assert callable(build)
                session.add(build(chain))
                await session.flush()


async def test_full_chain_is_accepted(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        chain = await _chain(session, workspace_id)
        session.add_all([_event(chain, 1), _event(chain, 2), _score(chain)])


async def test_duplicate_run_is_rejected(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    await _rejected(sessionmaker, workspace_id, _run)


async def test_duplicate_attempt_number_is_rejected(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    await _rejected(sessionmaker, workspace_id, lambda c: _attempt(c, 1))


async def test_duplicate_trace_sequence_is_rejected(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    await _rejected(sessionmaker, workspace_id, lambda c: _event(c, 1), lambda c: _event(c, 1))


async def test_duplicate_score_dimension_is_rejected(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    await _rejected(sessionmaker, workspace_id, _score, _score)


async def test_experiment_idempotency_key_is_unique_per_workspace(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    await _rejected(
        sessionmaker,
        workspace_id,
        lambda c: _experiment(c.workspace_id, c.dataset_version_id, idempotency_key="k"),
        lambda c: _experiment(c.workspace_id, c.dataset_version_id, idempotency_key="k"),
    )


async def test_unknown_experiment_mode_is_rejected(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    await _rejected(
        sessionmaker,
        workspace_id,
        lambda c: _experiment(c.workspace_id, c.dataset_version_id, mode="other"),
    )


async def test_row_cannot_point_into_another_workspace(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    other = models.Workspace(id=new_id("ws"), name="other")
    async with sessionmaker.begin() as session:
        session.add(other)
    await _rejected(sessionmaker, workspace_id, lambda c: _run(c, workspace_id=other.id))
