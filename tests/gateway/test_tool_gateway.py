from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.environments.contract import CallContext, FaultSpec, ToolArgs, ToolResult
from app.environments.registry import EnvironmentRegistry, default_registry
from app.environments.state_store import EnvStateStore
from app.environments.support_retail.environment import SupportRetail
from app.environments.support_retail.tools import TOOLS, IssueRefundArgs
from app.execution.trace_recorder import TraceRecorder
from app.gateway.tool_gateway import (
    AttemptContext,
    ToolGateway,
    idempotency_key,
    public_schemas,
)
from tests.environments.support_retail.fixtures import FIXTURE


@pytest.fixture
async def ctx(
    sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt
) -> AttemptContext:
    async with sessionmaker.begin() as session:
        await SupportRetail(EnvStateStore(session, attempt.workspace_id)).setup(
            attempt.env_id, FIXTURE
        )
    return AttemptContext(
        attempt_id=attempt.id,
        workspace_id=attempt.workspace_id,
        env_id=attempt.env_id,
        environment="support-retail@1",
        task_context={"authenticated_customer_id": "cust_12"},
    )


@pytest.fixture
def recorder(
    sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt
) -> TraceRecorder:
    return TraceRecorder(sessionmaker, attempt.workspace_id, attempt.id)


@pytest.fixture
def gateway(sessionmaker: async_sessionmaker[AsyncSession]) -> ToolGateway:
    return ToolGateway(sessionmaker, default_registry())


async def _trace(
    sessionmaker: async_sessionmaker[AsyncSession], attempt_id: str
) -> list[models.TraceEvent]:
    async with sessionmaker() as session:
        query = (
            select(models.TraceEvent)
            .where(models.TraceEvent.attempt_id == attempt_id)
            .order_by(models.TraceEvent.sequence_number)
        )
        return list((await session.scalars(query)).all())


async def _tool_calls(
    sessionmaker: async_sessionmaker[AsyncSession], attempt_id: str
) -> list[models.ToolCall]:
    async with sessionmaker() as session:
        query = select(models.ToolCall).where(models.ToolCall.attempt_id == attempt_id)
        return list((await session.scalars(query)).all())


async def _refunds(sessionmaker: async_sessionmaker[AsyncSession], ctx: AttemptContext) -> Any:
    async with sessionmaker() as session:
        state = await EnvStateStore(session, ctx.workspace_id).snapshot(ctx.env_id)
    # The fixture already holds one earlier refund, which tests leave alone.
    return [r for r in state.get("refunds", []) if r["refund_id"] != "ref_0001"]


REFUND = {"order_id": "ord_1001", "amount_minor": 24999, "approval_id": "apr_0001"}


async def test_successful_call_is_traced_and_linked(
    gateway: ToolGateway,
    ctx: AttemptContext,
    recorder: TraceRecorder,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    approval = {"order_id": "ord_1001", "amount_minor": 24999, "reason": "damaged"}
    await gateway.call(ctx, recorder, "request_refund_approval", approval)
    outcome = await gateway.call(ctx, recorder, "issue_refund", REFUND)
    assert outcome.status == "ok"
    requested, completed = (await _trace(sessionmaker, ctx.attempt_id))[2:]
    assert requested.event_type == "tool_call_requested"
    assert requested.payload["arguments"]["amount_minor"] == 24999
    assert completed.event_type == "tool_call_completed"
    assert completed.payload["tool_call_id"] == outcome.tool_call_id
    assert completed.payload["state_changes"][0]["record_id"] == "ref_0002"
    row = next(
        r for r in await _tool_calls(sessionmaker, ctx.attempt_id) if r.id == outcome.tool_call_id
    )
    assert (row.id, row.status) == (outcome.tool_call_id, "ok")
    assert (row.request_event_id, row.result_event_id) == (requested.id, completed.id)
    assert (await _refunds(sessionmaker, ctx))[0]["tool_call_id"] == outcome.tool_call_id


@pytest.mark.parametrize(
    ("tool", "arguments", "code"),
    [
        ("delete_everything", {}, "TOOL_NOT_ALLOWED"),
        (
            "issue_refund",
            {**REFUND, "amount_minor": "24999"},
            "INVALID_ARGUMENTS",
        ),
        (
            "issue_refund",
            {**REFUND, "customer_id": "cust_99"},
            "INVALID_ARGUMENTS",
        ),
        ("issue_refund", {"order_id": "ord_1001"}, "INVALID_ARGUMENTS"),
    ],
)
async def test_bad_calls_are_rejected_recorded_and_change_nothing(
    gateway: ToolGateway,
    ctx: AttemptContext,
    recorder: TraceRecorder,
    sessionmaker: async_sessionmaker[AsyncSession],
    tool: str,
    arguments: dict[str, Any],
    code: str,
) -> None:
    outcome = await gateway.call(ctx, recorder, tool, arguments)
    assert (outcome.status, outcome.error_code) == ("rejected", code)
    [row] = await _tool_calls(sessionmaker, ctx.attempt_id)
    assert (row.status, row.error_code) == ("rejected", code)
    assert len(await _trace(sessionmaker, ctx.attempt_id)) == 2
    assert await _refunds(sessionmaker, ctx) == []


async def test_tools_outside_the_allowlist_are_rejected(
    gateway: ToolGateway, ctx: AttemptContext, recorder: TraceRecorder
) -> None:
    limited = ctx.model_copy(update={"allowed_tools": frozenset({"get_order"})})
    outcome = await gateway.call(
        limited,
        recorder,
        "issue_refund",
        REFUND,
    )
    assert outcome.error_code == "TOOL_NOT_ALLOWED"


async def test_identity_comes_from_the_server_not_the_agent(
    gateway: ToolGateway, ctx: AttemptContext, recorder: TraceRecorder
) -> None:
    outcome = await gateway.call(ctx, recorder, "get_order", {"order_id": "ord_2002"})
    assert outcome.error_code == "ORDER_NOT_FOUND"


class _BrokenEnvironment(SupportRetail):
    async def execute(self, env_id: str, tool: str, args: ToolArgs, ctx: CallContext) -> ToolResult:
        await self._store.put(env_id, "refunds", "ref_9999", {"half": "done"})
        raise RuntimeError("boom")


async def test_failing_tool_rolls_back_its_effects(
    ctx: AttemptContext,
    recorder: TraceRecorder,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    gateway = ToolGateway(
        sessionmaker, EnvironmentRegistry({"support-retail@1": _BrokenEnvironment})
    )
    outcome = await gateway.call(ctx, recorder, "get_order", {"order_id": "ord_1001"})
    assert (outcome.status, outcome.error_code) == ("error", "TOOL_FAILED")
    assert await _refunds(sessionmaker, ctx) == []
    [row] = await _tool_calls(sessionmaker, ctx.attempt_id)
    assert row.error_code == "TOOL_FAILED"
    assert [e.sequence_number for e in await _trace(sessionmaker, ctx.attempt_id)] == [1, 2]


def test_public_schemas_respect_the_allowlist() -> None:
    [schema] = public_schemas(TOOLS, frozenset({"get_order"}))
    assert schema.name == "get_order"
    assert schema.parameters["additionalProperties"] is False


APPROVAL = {"order_id": "ord_1001", "amount_minor": 24999, "reason": "damaged"}


def _with_faults(ctx: AttemptContext, *faults: FaultSpec) -> AttemptContext:
    return ctx.model_copy(update={"faults": faults})


async def _completed(
    sessionmaker: async_sessionmaker[AsyncSession], attempt_id: str, tool_call_id: str
) -> dict[str, Any]:
    events = await _trace(sessionmaker, attempt_id)
    return next(
        e.payload
        for e in events
        if e.event_type == "tool_call_completed" and e.payload["tool_call_id"] == tool_call_id
    )


async def test_fault_before_the_effect_changes_nothing_and_the_retry_works(
    gateway: ToolGateway,
    ctx: AttemptContext,
    recorder: TraceRecorder,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    faulty = _with_faults(ctx, FaultSpec(tool="issue_refund", kind="timeout"))
    await gateway.call(faulty, recorder, "request_refund_approval", APPROVAL)
    first = await gateway.call(faulty, recorder, "issue_refund", REFUND)
    assert (first.status, first.error_code) == ("error", "TIMEOUT")
    assert await _refunds(sessionmaker, ctx) == []
    payload = await _completed(sessionmaker, ctx.attempt_id, first.tool_call_id)
    assert payload["fault"]["when"] == "before_effect"
    assert payload["state_changes"] == []
    second = await gateway.call(faulty, recorder, "issue_refund", REFUND)
    assert second.status == "ok"
    assert len(await _refunds(sessionmaker, ctx)) == 1


async def test_fault_after_the_effect_hides_a_real_refund_and_the_retry_replays_it(
    gateway: ToolGateway,
    ctx: AttemptContext,
    recorder: TraceRecorder,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    faulty = _with_faults(
        ctx, FaultSpec(tool="issue_refund", kind="service_error", when="after_effect")
    )
    await gateway.call(faulty, recorder, "request_refund_approval", APPROVAL)
    first = await gateway.call(faulty, recorder, "issue_refund", REFUND)
    assert (first.status, first.error_code, first.output) == (
        "error",
        "SERVICE_UNAVAILABLE",
        {"message": "The service is temporarily unavailable."},
    )
    [refund] = await _refunds(sessionmaker, ctx)
    payload = await _completed(sessionmaker, ctx.attempt_id, first.tool_call_id)
    assert payload["state_changes"][0]["record_id"] == refund["refund_id"]
    retry = await gateway.call(faulty, recorder, "issue_refund", REFUND)
    assert (retry.status, retry.output["refund_id"]) == ("ok", refund["refund_id"])
    retried = await _completed(sessionmaker, ctx.attempt_id, retry.tool_call_id)
    assert (retried["replayed"], retried["state_changes"]) == (True, [])
    assert len(await _refunds(sessionmaker, ctx)) == 1


async def test_repeating_a_successful_refund_never_pays_twice(
    gateway: ToolGateway,
    ctx: AttemptContext,
    recorder: TraceRecorder,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await gateway.call(ctx, recorder, "request_refund_approval", APPROVAL)
    first = await gateway.call(ctx, recorder, "issue_refund", REFUND)
    second = await gateway.call(ctx, recorder, "issue_refund", REFUND)
    assert first.output == second.output
    assert len(await _refunds(sessionmaker, ctx)) == 1


async def test_refused_calls_are_not_replayed(
    gateway: ToolGateway,
    ctx: AttemptContext,
    recorder: TraceRecorder,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    early = await gateway.call(ctx, recorder, "issue_refund", REFUND)
    assert early.error_code == "APPROVAL_NOT_FOUND"
    await gateway.call(ctx, recorder, "request_refund_approval", APPROVAL)
    later = await gateway.call(ctx, recorder, "issue_refund", REFUND)
    assert later.status == "ok"


async def test_faults_follow_the_nth_valid_call_of_a_tool(
    gateway: ToolGateway, ctx: AttemptContext, recorder: TraceRecorder
) -> None:
    faulty = _with_faults(ctx, FaultSpec(tool="get_order", occurrence=2, kind="service_error"))
    order = {"order_id": "ord_1001"}
    statuses = [
        (await gateway.call(faulty, recorder, "get_order", order)).status,
        # A rejected call is not an operation, so it does not move the count.
        (await gateway.call(faulty, recorder, "get_order", {"order": "x"})).status,
        (await gateway.call(faulty, recorder, "get_order", order)).status,
        (await gateway.call(faulty, recorder, "get_order", order)).status,
    ]
    assert statuses == ["ok", "rejected", "error", "ok"]


def test_idempotency_key_depends_on_attempt_tool_and_arguments() -> None:
    args = IssueRefundArgs.model_validate(REFUND)
    other = IssueRefundArgs.model_validate({**REFUND, "amount_minor": 1})
    key = idempotency_key("att_1", "issue_refund", args)
    assert key == idempotency_key("att_1", "issue_refund", IssueRefundArgs.model_validate(REFUND))
    assert key != idempotency_key("att_2", "issue_refund", args)
    assert key != idempotency_key("att_1", "issue_refund", other)
