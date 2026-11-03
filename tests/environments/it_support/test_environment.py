from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.environments.contract import CallContext, ToolResult
from app.environments.it_support.environment import ItSupport
from app.environments.it_support.tools import TOOLS
from app.environments.state_store import EnvStateStore
from tests.environments.it_support.fixtures import FIXTURE

ARGS = {t.name: t.args_model for t in TOOLS}


class World:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt):
        self.sessionmaker = sessionmaker
        self.attempt = attempt
        self.calls = 0

    async def setup(self) -> None:
        async with self.sessionmaker.begin() as session:
            await ItSupport(EnvStateStore(session, self.attempt.workspace_id)).setup(
                self.attempt.env_id, FIXTURE
            )

    async def call(self, tool: str, requester: str | None = "emp_1", **args: Any) -> ToolResult:
        self.calls += 1
        ctx = CallContext(
            attempt_id=self.attempt.id,
            workspace_id=self.attempt.workspace_id,
            env_id=self.attempt.env_id,
            tool_call_id=f"tc_{self.calls}",
            task_context={} if requester is None else {"authenticated_employee_id": requester},
            idempotency_key=f"key_{self.calls}",
        )
        async with self.sessionmaker.begin() as session:
            env = ItSupport(EnvStateStore(session, self.attempt.workspace_id))
            return await env.execute(
                self.attempt.env_id, tool, ARGS[tool].model_validate(args), ctx
            )

    async def snapshot(self) -> dict[str, Any]:
        async with self.sessionmaker.begin() as session:
            return await ItSupport(EnvStateStore(session, self.attempt.workspace_id)).snapshot(
                self.attempt.env_id
            )


@pytest.fixture
async def world(sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt) -> World:
    world = World(sessionmaker, attempt)
    await world.setup()
    return world


async def test_lookup_works_for_self_and_direct_reports_only(world: World) -> None:
    assert (await world.call("lookup_user", user_id="emp_1")).output["department"] == "engineering"
    assert (await world.call("lookup_user", user_id="emp_2")).status == "ok"
    other = await world.call("lookup_user", user_id="emp_9")
    missing = await world.call("lookup_user", user_id="emp_404")
    assert other == missing
    assert other.error_code == "USER_NOT_FOUND"
    assert "email" not in (await world.call("lookup_user", user_id="emp_1")).output


async def test_password_reset_only_for_yourself(world: World) -> None:
    own = await world.call("reset_password", user_id="emp_1")
    assert own.output == {"reset_id": "rst_0001", "sent_to": "rhea@corp.example"}
    report = await world.call("reset_password", user_id="emp_2")
    assert report.error_code == "USER_NOT_FOUND"
    assert len((await world.snapshot())["password_resets"]) == 1


async def test_suspended_accounts_get_no_reset_and_no_access(world: World) -> None:
    reset = await world.call("reset_password", requester="emp_3", user_id="emp_3")
    grant = await world.call("grant_access", user_id="emp_3", system="wiki")
    assert reset.error_code == grant.error_code == "ACCOUNT_SUSPENDED"


async def test_grants_follow_the_department_self_service_list(world: World) -> None:
    granted = await world.call("grant_access", user_id="emp_1", system="jira")
    assert granted.output["new"] is True
    assert [c.collection for c in granted.state_changes] == ["access_grants", "employees"]
    again = await world.call("grant_access", user_id="emp_1", system="jira")
    assert (again.output["new"], again.state_changes) == (False, [])
    payroll = await world.call("grant_access", user_id="emp_1", system="payroll")
    assert payroll.error_code == "NOT_SELF_SERVICE"
    assert [g["system"] for g in (await world.snapshot())["access_grants"]] == ["jira"]


async def test_managers_can_grant_their_reports_but_nobody_else(world: World) -> None:
    report = await world.call("grant_access", user_id="emp_2", system="wiki")
    boss = await world.call("grant_access", user_id="emp_9", system="wiki")
    assert report.status == "ok"
    assert boss.error_code == "USER_NOT_FOUND"


async def test_routing_sets_the_queue_of_your_own_ticket(world: World) -> None:
    routed = await world.call("route_ticket", ticket_id="it_1", queue="hardware", note="Screen")
    assert routed.output == {"ticket_id": "it_1", "queue": "hardware"}
    other = await world.call("route_ticket", ticket_id="it_9", queue="network", note="x")
    assert other.error_code == "TICKET_NOT_FOUND"
    queues = {t["ticket_id"]: t["queue"] for t in (await world.snapshot())["tickets"]}
    assert queues == {"it_1": "hardware", "it_9": None}


async def test_calls_without_a_signed_in_employee_are_refused(world: World) -> None:
    result = await world.call("lookup_user", requester=None, user_id="emp_1")
    assert result.error_code == "NOT_AUTHENTICATED"
