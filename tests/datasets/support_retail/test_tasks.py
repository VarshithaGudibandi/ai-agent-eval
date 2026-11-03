from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.content import load_task_file
from app.db.leakage import find_leaks
from app.environments.support_retail.records import RetailFixture
from app.environments.support_retail.tools import TOOLS
from app.evaluators.retail_spec import RetailSpec
from tests.datasets.support_retail.reference import run_reference

TASK_DIR = Path(__file__).parents[3] / "datasets/support_retail/tasks"
FILES = sorted(TASK_DIR.glob("*.json"))
TASKS = {path.stem: load_task_file(path) for path in FILES}

CATEGORY_COUNTS = {
    "lookup": 8,
    "eligible_refund": 9,
    "ineligible_refund": 8,
    "missing_information": 6,
    "access_boundary": 5,
    "tool_failure": 6,
    "duplicate_operation": 4,
    "untrusted_content": 4,
}


def test_the_dataset_has_fifty_tasks_in_the_planned_categories() -> None:
    assert Counter(t.review.category for t in TASKS.values()) == CATEGORY_COUNTS


def test_splits_are_sixty_twenty_twenty_and_every_category_is_in_each() -> None:
    assert Counter(t.split for t in TASKS.values()) == {"dev": 30, "validation": 10, "held_out": 10}
    for split in models.Split:
        assert {t.review.category for t in TASKS.values() if t.split == split} == set(
            CATEGORY_COUNTS
        )


def test_nothing_leaks_between_splits() -> None:
    assert find_leaks(list(TASKS.values())) == []


@pytest.mark.parametrize("name", list(TASKS))
def test_task_file_is_consistent(name: str) -> None:
    task = TASKS[name]
    assert name == task.task_key.replace("-", "_")
    RetailSpec.model_validate(task.evaluator_spec)
    tools = {t.name for t in TOOLS}
    assert {f.tool for f in task.faults} <= tools
    assert task.review.reference_script is not None
    assert {s.tool for s in task.review.reference_script.steps} <= tools
    # The agent's input never carries what only the grader should see.
    visible = task.agent_input.model_dump_json()
    assert "reviewer" not in visible and "expected" not in visible


@pytest.mark.parametrize("name", list(TASKS))
async def test_reference_script_passes_every_evaluator(
    name: str, sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    status, scores = await run_reference(sessionmaker, workspace_id, TASKS[name])
    failing = {
        s.dimension: (s.reason_code, s.evidence)
        for s in scores
        if s.status not in ("pass", "not_applicable")
    }
    assert status == models.EvaluationStatus.PASS, failing
    assert {s.status for s in scores} <= {"pass", "not_applicable"}


@pytest.mark.parametrize("name", list(TASKS))
def test_task_world_supports_its_expected_outcome(name: str) -> None:
    task = TASKS[name]
    world = RetailFixture.model_validate(task.fixture)
    spec = RetailSpec.model_validate(task.evaluator_spec)
    customer = task.agent_input.context["authenticated_customer_id"]
    assert customer in {c.customer_id for c in world.customers}
    orders = {o.order_id: o for o in world.orders}
    accepted = {r.order_id for r in world.returns if r.accepted}
    permitted = {p.order_id: p.max_amount_minor for p in world.reviewer.permitted}
    for want in spec.expected_refunds:
        order = orders[want.order_id]
        earlier = sum(r.amount_minor for r in world.refunds if r.order_id == order.order_id)
        # An expected refund must be one the rules allow: own order, accepted return,
        # within the balance, and approvable.
        assert order.customer_id == customer
        assert want.order_id in accepted
        assert want.amount_minor <= order.total_minor - earlier
        assert world.reviewer.decision == "approve"
        assert want.amount_minor <= permitted[want.order_id]
    tickets = {t.ticket_id: t for t in world.tickets}
    context_ticket = task.agent_input.context.get("ticket_id")
    if context_ticket is not None:
        assert tickets[context_ticket].customer_id == customer
    if spec.expected_ticket is not None:
        assert spec.expected_ticket.ticket_id == context_ticket
    for pattern in [*spec.required_calls, *spec.forbidden_calls]:
        if "order_id" in pattern.args:
            assert pattern.args["order_id"] in orders


def test_every_task_has_been_reviewed() -> None:
    assert all(
        t.review.reviewer is not None and t.review.reviewed_on is not None for t in TASKS.values()
    )
