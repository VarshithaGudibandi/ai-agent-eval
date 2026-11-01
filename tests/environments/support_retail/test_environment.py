import copy
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.environments.contract import CallContext, ToolResult
from app.environments.state_store import EnvStateStore
from app.environments.support_retail.environment import SupportRetail
from app.environments.support_retail.tools import TOOLS, IssueRefundArgs, UpdateTicketArgs
from tests.environments.support_retail.fixtures import FIXTURE

ARGS = {t.name: t.args_model for t in TOOLS}


class World:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt):
        self.sessionmaker = sessionmaker
        self.attempt = attempt
        self.calls = 0

    async def setup(self, fixture: dict[str, Any] = FIXTURE) -> None:
        async with self.sessionmaker.begin() as session:
            env = SupportRetail(EnvStateStore(session, self.attempt.workspace_id))
            await env.setup(self.attempt.env_id, fixture)

    async def call(self, tool: str, customer: str | None = "cust_12", **args: Any) -> ToolResult:
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
            return await env.execute(
                self.attempt.env_id, tool, ARGS[tool].model_validate(args), ctx
            )

    async def approve(self, order_id: str, amount_minor: int) -> ToolResult:
        return await self.call(
            "request_refund_approval", order_id=order_id, amount_minor=amount_minor, reason="r"
        )

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


def _fixture(**changes: Any) -> dict[str, Any]:
    fixture = copy.deepcopy(FIXTURE)
    fixture.update(changes)
    return fixture


async def test_get_order_returns_own_order_without_owner_field(world: World) -> None:
    result = await world.call("get_order", order_id="ord_1002")
    assert result.status == "ok"
    assert (result.output["total_minor"], result.output["refunded_minor"]) == (9000, 3000)
    assert "customer_id" not in result.output


@pytest.mark.parametrize(
    "tool", ["get_order", "get_return_status", "get_refund_status", "request_refund_approval"]
)
async def test_other_customers_order_looks_like_a_missing_one(world: World, tool: str) -> None:
    extra = {"amount_minor": 100, "reason": "r"} if tool == "request_refund_approval" else {}
    other = await world.call(tool, order_id="ord_2002", **extra)
    missing = await world.call(tool, order_id="ord_nope", **extra)
    assert other == missing
    assert other.error_code == "ORDER_NOT_FOUND"


async def test_return_status_reports_arrival_and_inspection(world: World) -> None:
    accepted = await world.call("get_return_status", order_id="ord_1001")
    in_transit = await world.call("get_return_status", order_id="ord_1002")
    assert (accepted.output["status"], accepted.output["inspection"]) == ("received", "passed")
    assert in_transit.output["status"] == "in_transit"


async def test_order_without_a_return_says_not_started(world: World) -> None:
    fixture = _fixture(returns=[])
    await world.teardown()
    await world.setup(fixture)
    result = await world.call("get_return_status", order_id="ord_1001")
    assert result.output == {"order_id": "ord_1001", "status": "not_started"}


async def test_refund_status_lists_refunds_and_what_is_left(world: World) -> None:
    result = await world.call("get_refund_status", order_id="ord_1002")
    assert result.output["refunded_minor"] == 3000
    assert result.output["refundable_minor"] == 6000
    assert [r["refund_id"] for r in result.output["refunds"]] == ["ref_0001"]


async def test_policy_search_finds_the_approval_rule(world: World) -> None:
    result = await world.call("search_policy", query="Do refunds need approval?")
    assert result.output["results"][0]["policy_id"] == "POL-3"


async def test_approved_refund_is_recorded_and_uses_up_its_approval(world: World) -> None:
    approval = await world.approve("ord_1001", 24999)
    assert approval.output["status"] == "approved"
    assert approval.output["approval_id"] == "apr_0001"
    result = await world.call(
        "issue_refund", order_id="ord_1001", amount_minor=24999, approval_id="apr_0001"
    )
    assert result.status == "ok"
    # One refund already exists in the fixture, so the new one is the second.
    assert result.output["refund_id"] == "ref_0002"
    assert [(c.collection, c.operation) for c in result.state_changes] == [
        ("refunds", "insert"),
        ("approvals", "update"),
    ]
    state = await world.snapshot()
    [refund] = [r for r in state["refunds"] if r["refund_id"] == "ref_0002"]
    assert (refund["approval_id"], refund["tool_call_id"]) == ("apr_0001", "tc_2")
    assert state["approvals"][0]["refund_id"] == "ref_0002"
    reused = await world.call(
        "issue_refund", order_id="ord_1001", amount_minor=24999, approval_id="apr_0001"
    )
    assert reused.error_code == "APPROVAL_ALREADY_USED"


async def test_refund_without_an_approval_is_refused(world: World) -> None:
    result = await world.call(
        "issue_refund", order_id="ord_1001", amount_minor=24999, approval_id="apr_9999"
    )
    assert result.error_code == "APPROVAL_NOT_FOUND"
    assert not [r for r in (await world.snapshot())["refunds"] if r["order_id"] == "ord_1001"]


async def test_approval_for_one_order_cannot_refund_another(world: World) -> None:
    await world.approve("ord_1001", 3000)
    result = await world.call(
        "issue_refund", order_id="ord_1002", amount_minor=3000, approval_id="apr_0001"
    )
    assert result.error_code == "APPROVAL_NOT_FOUND"


async def test_amount_must_match_the_approval(world: World) -> None:
    await world.approve("ord_1001", 20000)
    result = await world.call(
        "issue_refund", order_id="ord_1001", amount_minor=24999, approval_id="apr_0001"
    )
    assert result.error_code == "APPROVAL_MISMATCH"


async def test_reviewer_rejects_more_than_permitted(world: World) -> None:
    approval = await world.approve("ord_1001", 25000)
    assert approval.output["status"] == "rejected"
    result = await world.call(
        "issue_refund", order_id="ord_1001", amount_minor=25000, approval_id="apr_0001"
    )
    assert result.error_code == "APPROVAL_NOT_VALID"


async def test_reviewer_approves_a_smaller_amount_than_due(world: World) -> None:
    approval = await world.approve("ord_1001", 2499)
    assert approval.output["status"] == "approved"


async def test_second_approval_for_the_same_order_is_rejected(world: World) -> None:
    await world.approve("ord_1001", 24999)
    second = await world.approve("ord_1001", 24999)
    assert second.output["status"] == "rejected"


async def test_reviewer_rejects_orders_it_was_not_told_to_permit(world: World) -> None:
    fixture = _fixture(reviewer={"decision": "approve", "permitted": []})
    await world.teardown()
    await world.setup(fixture)
    assert (await world.approve("ord_1001", 24999)).output["status"] == "rejected"


@pytest.mark.parametrize(("decision", "status"), [("reject", "rejected"), ("expire", "expired")])
async def test_reviewer_can_reject_or_let_a_request_expire(
    world: World, decision: str, status: str
) -> None:
    reviewer = {**FIXTURE["reviewer"], "decision": decision}
    await world.teardown()
    await world.setup(_fixture(reviewer=reviewer))
    approval = await world.approve("ord_1001", 24999)
    assert approval.output["status"] == status
    assert (await world.snapshot())["approvals"][0]["simulated"] is True


async def test_refund_beyond_what_is_left_is_refused(world: World) -> None:
    await world.approve("ord_1002", 9000)
    result = await world.call(
        "issue_refund", order_id="ord_1002", amount_minor=9000, approval_id="apr_0001"
    )
    assert result.error_code == "REFUND_EXCEEDS_BALANCE"
    assert len((await world.snapshot())["refunds"]) == 1


async def test_cannot_refund_another_customers_order(world: World) -> None:
    result = await world.call(
        "issue_refund", order_id="ord_2002", amount_minor=100, approval_id="apr_0001"
    )
    assert result.error_code == "ORDER_NOT_FOUND"


async def test_ticket_update_sets_status_and_keeps_the_note(world: World) -> None:
    result = await world.call("update_ticket", ticket_id="tkt_1", status="resolved", note="Done")
    assert result.output["status"] == "resolved"
    [ticket] = [t for t in (await world.snapshot())["tickets"] if t["ticket_id"] == "tkt_1"]
    assert (ticket["status"], ticket["notes"]) == ("resolved", ["Done"])


async def test_cannot_update_another_customers_ticket(world: World) -> None:
    result = await world.call("update_ticket", ticket_id="tkt_9", status="resolved", note="x")
    assert result.error_code == "TICKET_NOT_FOUND"


async def test_calls_without_a_signed_in_customer_are_refused(world: World) -> None:
    result = await world.call("get_order", customer=None, order_id="ord_1001")
    assert result.error_code == "NOT_AUTHENTICATED"


async def test_snapshot_hides_the_reviewer_and_teardown_removes_state(world: World) -> None:
    state = await world.snapshot()
    assert set(state) == {"customers", "orders", "returns", "refunds", "approvals", "tickets"}
    await world.teardown()
    assert all(records == [] for records in (await world.snapshot()).values())


def test_arguments_are_strict() -> None:
    with pytest.raises(ValidationError):
        IssueRefundArgs.model_validate(
            {"order_id": "ord_1001", "amount_minor": "100", "approval_id": "apr_0001"}
        )
    with pytest.raises(ValidationError):
        UpdateTicketArgs.model_validate({"ticket_id": "tkt_1", "status": "closed", "note": "x"})


async def test_invalid_fixture_is_rejected(world: World) -> None:
    with pytest.raises(ValidationError):
        await world.setup({"customers": [], "orders": [], "secret": True})
