from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EMPLOYEES = "employees"
DEPARTMENTS = "departments"
TICKETS = "tickets"
PASSWORD_RESETS = "password_resets"  # noqa: S105 - a collection name, not a secret
ACCESS_GRANTS = "access_grants"

SNAPSHOT_COLLECTIONS = (EMPLOYEES, DEPARTMENTS, TICKETS, PASSWORD_RESETS, ACCESS_GRANTS)

Queue = Literal["hardware", "network", "access_review", "security"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Employee(Record):
    user_id: str
    name: str
    email: str
    department: str
    status: Literal["active", "suspended"]
    manager_id: str | None = None
    access: list[str] = Field(default_factory=list)
    # Free text from the directory. It is untrusted and may carry injected instructions.
    profile_note: str = ""


class Department(Record):
    name: str
    # Systems anyone in the department may get without an access review.
    self_service: list[str]


class Ticket(Record):
    ticket_id: str
    requester_id: str
    subject: str
    queue: Queue | None = None
    notes: list[str] = Field(default_factory=list)


class PasswordReset(Record):
    reset_id: str
    user_id: str
    sent_to: str
    tool_call_id: str


class AccessGrant(Record):
    grant_id: str
    user_id: str
    system: str
    tool_call_id: str


class ItFixture(Record):
    employees: list[Employee]
    departments: list[Department]
    tickets: list[Ticket] = Field(default_factory=list)
