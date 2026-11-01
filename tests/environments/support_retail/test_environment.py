from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.environments.contract import CallContext, ToolArgs, ToolResult
from app.environments.state_store import EnvStateStore
from app.environments.support_retail.environment import (
    GetOrderArgs,
    IssueRefundArgs,
    SupportRetail,
)
from tests.environments.support_retail.fixtures import FIXTURE


class World:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt):
        self.sessionmaker = sessionmaker
        self.attempt = attempt
        self.calls = 0

    async def setup(self, fixture: dict[str, Any] = FIXTURE) -> None:
        async with self.sessionmaker.begin() as session:
            env = SupportRetail(EnvStateStore(session, self.attempt.workspace_id))
            await env.setup(self.attempt.env_id, fixture)

    async def call(self, tool: str, args: ToolArgs, customer: str | None = "cust_12") -> ToolResult:
        self.calls += 1
        context = {} if customer is None else {"authenticated_customer_id": customer}
        ctx = CallContext(
            attempt_id=self.attempt.id,
            workspace_id=self.attempt.workspace_id,
            env_id=self.attempt.env_id,
            tool_call_id=f"tc_{self.calls}",
            task_context=context,
        )
        async with self.sessionmaker.begin() as session:
            env = SupportRetail(EnvStateStore(session, self.attempt.workspace_id))
            return await env.execute(self.attempt.env_id, tool, args, ctx)

    async def snapshot(self) -> dict[str, Any]:
        async with self.sessionmaker.begin() as session:
            env = SupportRetail(EnvStateStore(session, self.attempt.workspace_id))
            return await env.snapshot(self.attempt.env_id)

    async def teardown(self) -> None:
        async with self.sessionmaker.begin() as session:
            env = SupportRetail(EnvStateStore(session, self.attempt.workspace_id))
            await env.teardown(self.attempt.env_id)


@pytest.fixture
async def world(sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt) -> World:
    world = World(sessionmaker, attempt)
    await world.setup()
    return world


async def test_get_order_returns_own_order_without_owner_field(world: World) -> None:
    result = await world.call("get_order", GetOrderArgs(order_id="ord_1001"))
    assert result.status == "ok"
    assert result.output["total_minor"] == 24999
    assert result.output["refunded_minor"] == 0
    assert "customer_id" not in result.output


async def test_other_customers_order_looks_like_a_missing_one(world: World) -> None:
    other = await world.call("get_order", GetOrderArgs(order_id="ord_2002"))
    missing = await world.call("get_order", GetOrderArgs(order_id="ord_nope"))
    assert other == missing
    assert other.error_code == "ORDER_NOT_FOUND"


async def test_refund_is_recorded_with_its_tool_call(world: World) -> None:
    args = IssueRefundArgs(order_id="ord_1001", amount_minor=24999, reason="arrived broken")
    result = await world.call("issue_refund", args)
    assert result.status == "ok"
    assert [c.record_id for c in result.state_changes] == ["ref_0001"]
    refunds = (await world.snapshot())["refunds"]
    assert refunds == [
        {
            "refund_id": "ref_0001",
            "order_id": "ord_1001",
            "amount_minor": 24999,
            "currency": "USD",
            "reason": "arrived broken",
            "tool_call_id": "tc_1",
        }
    ]
    order = await world.call("get_order", GetOrderArgs(order_id="ord_1001"))
    assert order.output["refunded_minor"] == 24999


async def test_refund_beyond_balance_is_refused_and_changes_nothing(world: World) -> None:
    await world.call(
        "issue_refund", IssueRefundArgs(order_id="ord_1001", amount_minor=20000, reason="r")
    )
    result = await world.call(
        "issue_refund", IssueRefundArgs(order_id="ord_1001", amount_minor=5000, reason="r")
    )
    assert result.error_code == "REFUND_EXCEEDS_BALANCE"
    assert len((await world.snapshot())["refunds"]) == 1


async def test_cannot_refund_another_customers_order(world: World) -> None:
    result = await world.call(
        "issue_refund", IssueRefundArgs(order_id="ord_2002", amount_minor=100, reason="r")
    )
    assert result.error_code == "ORDER_NOT_FOUND"
    assert (await world.snapshot())["refunds"] == []


async def test_calls_without_a_signed_in_customer_are_refused(world: World) -> None:
    result = await world.call("get_order", GetOrderArgs(order_id="ord_1001"), customer=None)
    assert result.error_code == "NOT_AUTHENTICATED"


async def test_teardown_removes_live_state(world: World) -> None:
    await world.teardown()
    assert await world.snapshot() == {"customers": [], "orders": [], "refunds": []}


def test_arguments_are_strict() -> None:
    with pytest.raises(ValidationError):
        IssueRefundArgs.model_validate(
            {"order_id": "ord_1001", "amount_minor": "100", "reason": "r"}
        )
    with pytest.raises(ValidationError):
        GetOrderArgs.model_validate({"order_id": "ord_1001", "customer_id": "cust_99"})


async def test_invalid_fixture_is_rejected(world: World) -> None:
    with pytest.raises(ValidationError):
        await world.setup({"customers": [], "orders": [], "secret": True})
