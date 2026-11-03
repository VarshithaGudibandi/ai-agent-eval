from pydantic import Field

from app.environments.contract import ToolArgs, ToolSchema
from app.environments.it_support.records import Queue

USER_ID = Field(min_length=1, max_length=100, description="The employee id, such as emp_12")


class LookupUserArgs(ToolArgs):
    user_id: str = USER_ID


class ResetPasswordArgs(ToolArgs):
    user_id: str = USER_ID


class GrantAccessArgs(ToolArgs):
    user_id: str = USER_ID
    system: str = Field(min_length=1, max_length=100, description="The system, such as jira")


class RouteTicketArgs(ToolArgs):
    ticket_id: str = Field(min_length=1, max_length=100, description="The ticket id")
    queue: Queue
    note: str = Field(min_length=1, max_length=1000, description="Why it goes to this queue")


TOOLS = (
    ToolSchema(
        name="lookup_user",
        description=(
            "Look up an employee: department, account status, manager and current access. "
            "Works for the signed-in employee and their direct reports."
        ),
        args_model=LookupUserArgs,
    ),
    ToolSchema(
        name="reset_password",
        description="Send a password reset link to the signed-in employee's own work email.",
        args_model=ResetPasswordArgs,
        effectful=True,
    ),
    ToolSchema(
        name="grant_access",
        description=(
            "Give an employee access to a system on their department's self-service list. "
            "Managers can do this for their direct reports."
        ),
        args_model=GrantAccessArgs,
        effectful=True,
    ),
    ToolSchema(
        name="route_ticket",
        description=(
            "Send the employee's ticket to a queue: hardware, network, access_review or security."
        ),
        args_model=RouteTicketArgs,
        effectful=True,
    ),
)
