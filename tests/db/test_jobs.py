import asyncio

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.contract import RunLimits
from app.db import models
from app.db.content import create_agent_config, create_dataset_version, load_task_file
from app.db.experiments import create_experiment, transition
from app.db.jobs import Claim, cancel_work, claim_next, reconcile_expired, renew_lease
from app.db.scope import WorkspaceScope
from tests.conftest import TASK_FILE

Sessions = async_sessionmaker[AsyncSession]


async def _experiment(sessionmaker: Sessions, workspace_id: str, repetitions: int = 2) -> str:
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        version = await create_dataset_version(scope, "d", [load_task_file(TASK_FILE)])
        configs = [
            await create_agent_config(scope, n, models.AdapterType.SCRIPTED, {"n": n}, RunLimits())
            for n in ("a", "b")
        ]
        experiment = await create_experiment(
            scope,
            version.id,
            [c.id for c in configs],
            [],
            mode=models.ExperimentMode.DEMO,
            repetitions=repetitions,
        )
        return experiment.id


async def _claim(sessionmaker: Sessions, experiment_id: str, worker: str = "w1") -> Claim | None:
    async with sessionmaker.begin() as session:
        return await claim_next(session, worker, 30, experiment_id=experiment_id)


async def _expire(sessionmaker: Sessions, attempt_id: str) -> None:
    async with sessionmaker.begin() as session:
        await session.execute(
            update(models.Attempt)
            .where(models.Attempt.id == attempt_id)
            .values(lease_expires_at=func.now() - func.make_interval(0, 0, 0, 0, 0, 0, 1))
        )


async def _row[T: models.WorkspaceScoped](sessionmaker: Sessions, model: type[T], row_id: str) -> T:
    async with sessionmaker() as session:
        return await session.get_one(model, row_id)


async def test_each_run_gets_a_job_and_configs_alternate(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    experiment_id = await _experiment(sessionmaker, workspace_id)
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(models.Job.position, models.Run.agent_config_id, models.Run.repetition)
                .join(models.Run, models.Run.id == models.Job.run_id)
                .where(models.Job.experiment_id == experiment_id)
                .order_by(models.Job.position)
            )
        ).all()
    assert [r.position for r in rows] == [1, 2, 3, 4]
    configs = [r.agent_config_id for r in rows]
    assert configs[0] != configs[1] and configs[0] == configs[2]
    assert [r.repetition for r in rows] == [1, 1, 2, 2]


async def test_claim_starts_an_attempt_holding_a_lease(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    experiment_id = await _experiment(sessionmaker, workspace_id)
    claim = await _claim(sessionmaker, experiment_id)
    assert claim is not None and claim.workspace_id == workspace_id
    attempt = await _row(sessionmaker, models.Attempt, claim.attempt_id)
    job = await _row(sessionmaker, models.Job, claim.job_id)
    assert (attempt.lease_token, attempt.worker_id, job.status) == (
        claim.lease_token,
        "w1",
        "running",
    )
    assert attempt.lease_expires_at is not None


async def test_paused_experiments_hand_out_no_work(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    experiment_id = await _experiment(sessionmaker, workspace_id)
    async with sessionmaker.begin() as session:
        await transition(
            WorkspaceScope(session, workspace_id),
            experiment_id,
            {models.ExperimentStatus.QUEUED},
            models.ExperimentStatus.PAUSED_USER,
        )
    assert await _claim(sessionmaker, experiment_id) is None


async def test_concurrent_workers_never_claim_the_same_job(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    experiment_id = await _experiment(sessionmaker, workspace_id, repetitions=3)
    claims = await asyncio.gather(
        *(_claim(sessionmaker, experiment_id, f"w{i}") for i in range(20))
    )
    got = [c for c in claims if c is not None]
    assert len(got) == 6
    assert len({c.job_id for c in got}) == len({c.run_id for c in got}) == 6


async def test_a_lease_renews_only_for_its_holder_while_live(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    experiment_id = await _experiment(sessionmaker, workspace_id)
    claim = await _claim(sessionmaker, experiment_id)
    assert claim is not None

    async def renew(token: str) -> bool:
        async with sessionmaker.begin() as session:
            return await renew_lease(session, workspace_id, claim.attempt_id, token, 30)

    assert await renew(claim.lease_token)
    assert not await renew("someone-else")
    await _expire(sessionmaker, claim.attempt_id)
    assert not await renew(claim.lease_token)


async def test_expired_attempts_are_interrupted_and_retried_with_a_fresh_environment(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    experiment_id = await _experiment(sessionmaker, workspace_id, repetitions=1)
    first = await _claim(sessionmaker, experiment_id)
    assert first is not None
    await _expire(sessionmaker, first.attempt_id)
    async with sessionmaker.begin() as session:
        interrupted = await reconcile_expired(session, max_attempts=3)
    assert first.attempt_id in interrupted
    old = await _row(sessionmaker, models.Attempt, first.attempt_id)
    assert (old.execution_status, old.lease_token) == ("interrupted", None)
    assert (await _row(sessionmaker, models.Run, first.run_id)).execution_status == "queued"
    again = await _claim(sessionmaker, experiment_id)
    assert again is not None and again.run_id == first.run_id
    new = await _row(sessionmaker, models.Attempt, again.attempt_id)
    assert new.attempt_number == 2
    assert new.env_id != old.env_id


async def test_a_run_is_given_up_after_three_interrupted_attempts(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    experiment_id = await _experiment(sessionmaker, workspace_id, repetitions=1)
    for _ in range(2):
        while (claim := await _claim(sessionmaker, experiment_id)) is not None:
            await _expire(sessionmaker, claim.attempt_id)
            run_id = claim.run_id
        async with sessionmaker.begin() as session:
            await reconcile_expired(session, max_attempts=2)
    run = await _row(sessionmaker, models.Run, run_id)
    assert run.execution_status == "infrastructure_error"
    assert await _claim(sessionmaker, experiment_id) is None


async def test_cancelling_revokes_in_flight_leases_and_drops_queued_work(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    experiment_id = await _experiment(sessionmaker, workspace_id)
    claim = await _claim(sessionmaker, experiment_id)
    assert claim is not None
    async with sessionmaker.begin() as session:
        assert await cancel_work(WorkspaceScope(session, workspace_id), experiment_id) == 1
    attempt = await _row(sessionmaker, models.Attempt, claim.attempt_id)
    assert (attempt.execution_status, attempt.lease_token) == ("cancelled", None)
    async with sessionmaker.begin() as session:
        assert not await renew_lease(session, workspace_id, claim.attempt_id, claim.lease_token, 30)
    async with sessionmaker() as session:
        statuses = set(
            await session.scalars(
                select(models.Job.status).where(models.Job.experiment_id == experiment_id)
            )
        )
    assert statuses == {"cancelled"}
