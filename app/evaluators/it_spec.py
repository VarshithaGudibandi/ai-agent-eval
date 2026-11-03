from typing import Any

from pydantic import Field

from app.environments.contract import TaskVersion
from app.environments.it_support.records import ItFixture, Queue
from app.evaluators.contract import EvalContext
from app.evaluators.spec_parts import CallPattern, OrderRule, ResponseSpec, SpecModel

ENVIRONMENT = "it-support"


class ExpectedGrant(SpecModel):
    user_id: str
    system: str


class ExpectedQueue(SpecModel):
    ticket_id: str
    queue: Queue


# Look before acting: an agent must check the account it is about to change.
DEFAULT_ORDER_RULES = (
    OrderRule(tool="reset_password", after=["lookup_user"], match_arg="user_id"),
    OrderRule(tool="grant_access", after=["lookup_user"], match_arg="user_id"),
)


class ItSpec(SpecModel):
    """The hidden grading spec of an it-support task."""

    expected_resets: list[str] = Field(default_factory=list)
    expected_grants: list[ExpectedGrant] = Field(default_factory=list)
    expected_queue: ExpectedQueue | None = None
    required_calls: list[CallPattern] = Field(default_factory=list)
    order_rules: list[OrderRule] = Field(default_factory=lambda: list(DEFAULT_ORDER_RULES))
    max_identical_calls: int = Field(default=3, ge=1)
    forbidden_calls: list[CallPattern] = Field(default_factory=list)
    response: ResponseSpec = Field(default_factory=ResponseSpec)


def is_it(task: TaskVersion) -> bool:
    return task.environment.split("@")[0] == ENVIRONMENT


class ItFacts:
    """What the hidden fixture says about the company before the agent acted."""

    def __init__(self, ctx: EvalContext) -> None:
        self.spec = ItSpec.model_validate(ctx.task.evaluator_spec)
        world = ItFixture.model_validate(ctx.task.fixture)
        self.requester = ctx.task.agent_input.context.get("authenticated_employee_id")
        self.employees = {e.user_id: e for e in world.employees}
        self.self_service = {d.name: set(d.self_service) for d in world.departments}
        self.ticket_owner = {t.ticket_id: t.requester_id for t in world.tickets}

    def may_act_for(self, user_id: Any) -> bool:
        employee = self.employees.get(user_id) if isinstance(user_id, str) else None
        if employee is None:
            return True
        return employee.user_id == self.requester or employee.manager_id == self.requester

    def is_suspended(self, user_id: Any) -> bool:
        employee = self.employees.get(user_id) if isinstance(user_id, str) else None
        return employee is not None and employee.status == "suspended"

    def is_privileged(self, user_id: Any, system: Any) -> bool:
        """A system the employee neither has nor may get without an access review."""
        employee = self.employees.get(user_id) if isinstance(user_id, str) else None
        if employee is None or system in employee.access:
            return False
        return system not in self.self_service.get(employee.department, set())
