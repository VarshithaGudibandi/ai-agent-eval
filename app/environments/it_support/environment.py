from typing import Any

from pydantic import BaseModel

from app.environments.contract import (
    AgentTaskInput,
    CallContext,
    StateChange,
    TaskVersion,
    ToolArgs,
    ToolResult,
    ToolSchema,
)
from app.environments.it_support.records import (
    ACCESS_GRANTS,
    DEPARTMENTS,
    EMPLOYEES,
    PASSWORD_RESETS,
    SNAPSHOT_COLLECTIONS,
    TICKETS,
    AccessGrant,
    Department,
    Employee,
    ItFixture,
    PasswordReset,
    Ticket,
)
from app.environments.it_support.tools import (
    TOOLS,
    GrantAccessArgs,
    LookupUserArgs,
    ResetPasswordArgs,
    RouteTicketArgs,
)
from app.environments.state_store import EnvStateStore

# Someone else's account answers exactly like a missing one, so ids cannot be probed.
USER_NOT_FOUND = ToolResult.error("USER_NOT_FOUND", "No such employee you can act for.")
TICKET_NOT_FOUND = ToolResult.error("TICKET_NOT_FOUND", "No such ticket for this employee.")


def _change(collection: str, record_id: str, operation: str, record: BaseModel) -> StateChange:
    return StateChange.model_validate(
        {
            "collection": collection,
            "record_id": record_id,
            "operation": operation,
            "data": record.model_dump(),
        }
    )


class ItSupport:
    """A small company IT helpdesk: accounts, password resets, system access and tickets.

    The environment enforces who may act for whom, suspended accounts and department
    self-service lists. Which queue a ticket belongs in is a judgement, so only the
    evaluators grade it.
    """

    name = "it-support"
    version = "1"

    def __init__(self, store: EnvStateStore) -> None:
        self._store = store

    def tool_schemas(self) -> list[ToolSchema]:
        return list(TOOLS)

    def agent_view(self, task: TaskVersion) -> AgentTaskInput:
        return task.agent_input

    async def setup(self, env_id: str, fixture: dict[str, Any]) -> None:
        world = ItFixture.model_validate(fixture)
        records: list[tuple[str, str, BaseModel]] = [
            *((EMPLOYEES, e.user_id, e) for e in world.employees),
            *((DEPARTMENTS, d.name, d) for d in world.departments),
            *((TICKETS, t.ticket_id, t) for t in world.tickets),
        ]
        for collection, record_id, record in records:
            await self._store.put(env_id, collection, record_id, record.model_dump())

    async def execute(self, env_id: str, tool: str, args: ToolArgs, ctx: CallContext) -> ToolResult:
        requester_id = ctx.task_context.get("authenticated_employee_id")
        requester = await self._employee(env_id, requester_id)
        if requester is None:
            return ToolResult.error("NOT_AUTHENTICATED", "No signed-in employee.")
        match args:
            case LookupUserArgs():
                return await self._lookup(env_id, requester, args)
            case ResetPasswordArgs():
                return await self._reset(env_id, requester, args, ctx)
            case GrantAccessArgs():
                return await self._grant(env_id, requester, args, ctx)
            case RouteTicketArgs():
                return await self._route(env_id, requester, args)
        raise ValueError(f"it-support has no tool {tool!r}")

    async def snapshot(self, env_id: str) -> dict[str, Any]:
        state = await self._store.snapshot(env_id)
        return {name: state.get(name, []) for name in SNAPSHOT_COLLECTIONS}

    async def teardown(self, env_id: str) -> None:
        await self._store.clear(env_id)

    async def _employee(
        self, env_id: str, user_id: Any, *, for_update: bool = False
    ) -> Employee | None:
        if not isinstance(user_id, str):
            return None
        data = await self._store.get(env_id, EMPLOYEES, user_id, for_update=for_update)
        return Employee.model_validate(data) if data is not None else None

    async def _subject(
        self, env_id: str, requester: Employee, user_id: str, *, for_update: bool = False
    ) -> Employee | None:
        """The employee the requester may act for: themselves or a direct report."""
        employee = await self._employee(env_id, user_id, for_update=for_update)
        if employee is None:
            return None
        if employee.user_id == requester.user_id or employee.manager_id == requester.user_id:
            return employee
        return None

    async def _next_id(self, env_id: str, collection: str, field: str, prefix: str) -> str:
        taken = {r[field] for r in await self._store.list_records(env_id, collection)}
        number = len(taken) + 1
        while f"{prefix}_{number:04d}" in taken:
            number += 1
        return f"{prefix}_{number:04d}"

    async def _lookup(self, env_id: str, requester: Employee, args: LookupUserArgs) -> ToolResult:
        employee = await self._subject(env_id, requester, args.user_id)
        if employee is None:
            return USER_NOT_FOUND
        return ToolResult.ok(employee.model_dump(exclude={"email"}))

    async def _reset(
        self, env_id: str, requester: Employee, args: ResetPasswordArgs, ctx: CallContext
    ) -> ToolResult:
        # Even a manager cannot reset a report's password: the reset link would prove nothing
        # about who is asking.
        if args.user_id != requester.user_id:
            return USER_NOT_FOUND
        if requester.status == "suspended":
            return ToolResult.error("ACCOUNT_SUSPENDED", "The account is suspended.")
        reset = PasswordReset(
            reset_id=await self._next_id(env_id, PASSWORD_RESETS, "reset_id", "rst"),
            user_id=requester.user_id,
            sent_to=requester.email,
            tool_call_id=ctx.tool_call_id,
        )
        await self._store.put(env_id, PASSWORD_RESETS, reset.reset_id, reset.model_dump())
        return ToolResult.ok(
            {"reset_id": reset.reset_id, "sent_to": reset.sent_to},
            [_change(PASSWORD_RESETS, reset.reset_id, "insert", reset)],
        )

    async def _grant(
        self, env_id: str, requester: Employee, args: GrantAccessArgs, ctx: CallContext
    ) -> ToolResult:
        employee = await self._subject(env_id, requester, args.user_id, for_update=True)
        if employee is None:
            return USER_NOT_FOUND
        if employee.status == "suspended":
            return ToolResult.error("ACCOUNT_SUSPENDED", "The account is suspended.")
        if args.system in employee.access:
            return ToolResult.ok({"user_id": employee.user_id, "system": args.system, "new": False})
        data = await self._store.get(env_id, DEPARTMENTS, employee.department)
        department = Department.model_validate(data) if data is not None else None
        if department is None or args.system not in department.self_service:
            return ToolResult.error(
                "NOT_SELF_SERVICE", f"{args.system} needs an access review for this department."
            )
        grant = AccessGrant(
            grant_id=await self._next_id(env_id, ACCESS_GRANTS, "grant_id", "grt"),
            user_id=employee.user_id,
            system=args.system,
            tool_call_id=ctx.tool_call_id,
        )
        employee.access.append(args.system)
        await self._store.put(env_id, ACCESS_GRANTS, grant.grant_id, grant.model_dump())
        await self._store.put(env_id, EMPLOYEES, employee.user_id, employee.model_dump())
        return ToolResult.ok(
            {"user_id": employee.user_id, "system": args.system, "new": True},
            [
                _change(ACCESS_GRANTS, grant.grant_id, "insert", grant),
                _change(EMPLOYEES, employee.user_id, "update", employee),
            ],
        )

    async def _route(self, env_id: str, requester: Employee, args: RouteTicketArgs) -> ToolResult:
        data = await self._store.get(env_id, TICKETS, args.ticket_id, for_update=True)
        ticket = Ticket.model_validate(data) if data is not None else None
        if ticket is None or ticket.requester_id != requester.user_id:
            return TICKET_NOT_FOUND
        ticket.queue = args.queue
        ticket.notes.append(args.note)
        await self._store.put(env_id, TICKETS, ticket.ticket_id, ticket.model_dump())
        return ToolResult.ok(
            {"ticket_id": ticket.ticket_id, "queue": ticket.queue},
            [_change(TICKETS, ticket.ticket_id, "update", ticket)],
        )
