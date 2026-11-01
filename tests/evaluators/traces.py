import copy
from typing import Any

from app.environments.contract import AgentTaskInput, TaskVersion
from app.evaluators.contract import EvalContext, Score, TraceEvent
from tests.environments.support_retail.fixtures import FIXTURE

KEYS = {"refunds": "refund_id", "approvals": "approval_id", "tickets": "ticket_id"}


def retail_task(spec: dict[str, Any], fixture: dict[str, Any] | None = None) -> TaskVersion:
    return TaskVersion(
        id="task_1",
        task_key="t",
        environment="support-retail@1",
        scenario_family="refund",
        split="dev",
        agent_input=AgentTaskInput(
            user_message="m",
            context={"authenticated_customer_id": "cust_12", "ticket_id": "tkt_1"},
        ),
        fixture=fixture or FIXTURE,
        evaluator_spec=spec,
    )


class Trace:
    """Builds a trace shaped exactly like the gateway's, and the state it leads to."""

    def __init__(self, fixture: dict[str, Any] | None = None) -> None:
        self.fixture = fixture or FIXTURE
        self.events: list[TraceEvent] = []
        self.calls = 0
        self.state: dict[str, list[dict[str, Any]]] = {
            name: copy.deepcopy(self.fixture.get(name, []))
            for name in ("customers", "orders", "returns", "refunds", "approvals", "tickets")
        }

    def _event(self, event_type: str, payload: dict[str, Any]) -> str:
        event = TraceEvent(
            id=f"evt_{len(self.events) + 1}",
            sequence_number=len(self.events) + 1,
            event_type=event_type,
            payload=payload,
        )
        self.events.append(event)
        return event.id

    def call(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        status: str = "ok",
        output: dict[str, Any] | None = None,
        error_code: str | None = None,
        changes: list[dict[str, Any]] | None = None,
        fault: dict[str, Any] | None = None,
        replayed: bool = False,
    ) -> str:
        self.calls += 1
        tool_call_id = f"tc_{self.calls}"
        self._event(
            "tool_call_requested",
            {"tool_call_id": tool_call_id, "tool": tool, "arguments": arguments},
        )
        for change in changes or []:
            if change["collection"] == "refunds":
                change["data"].setdefault("tool_call_id", tool_call_id)
            records = self.state[change["collection"]]
            key = KEYS[change["collection"]]
            records[:] = [r for r in records if r[key] != change["record_id"]]
            records.append(change["data"])
        self._event(
            "tool_call_completed",
            {
                "tool_call_id": tool_call_id,
                "tool": tool,
                "status": status,
                "output": output or {},
                "error_code": error_code,
                "state_changes": changes or [],
                "fault": fault,
                "replayed": replayed,
                "duration_ms": 1,
            },
        )
        return tool_call_id

    def approve(self, order_id: str, amount: int, status: str = "approved", number: int = 1) -> str:
        approval = {
            "approval_id": f"apr_{number:04d}",
            "order_id": order_id,
            "amount_minor": amount,
            "reason": "r",
            "status": status,
            "refund_id": None,
            "tool_call_id": f"tc_{self.calls + 1}",
            "simulated": True,
        }
        self.call(
            "request_refund_approval",
            {"order_id": order_id, "amount_minor": amount, "reason": "r"},
            output={"approval_id": approval["approval_id"], "status": status},
            changes=[insert("approvals", approval["approval_id"], approval)],
        )
        return str(approval["approval_id"])

    def refund(
        self, order_id: str, amount: int, approval_id: str, number: int, **kwargs: Any
    ) -> str:
        refund = {
            "refund_id": f"ref_{number:04d}",
            "order_id": order_id,
            "amount_minor": amount,
            "currency": "USD",
            "approval_id": approval_id,
        }
        kwargs.setdefault("output", {"refund_id": refund["refund_id"]})
        kwargs.setdefault("changes", [insert("refunds", refund["refund_id"], refund)])
        return self.call(
            "issue_refund",
            {"order_id": order_id, "amount_minor": amount, "approval_id": approval_id},
            **kwargs,
        )

    def context(self, spec: dict[str, Any], response: str = "Done.") -> EvalContext:
        self._event("agent_finished", {"final_response": response, "stop_reason": "model_final"})
        return EvalContext(
            task=retail_task(spec, self.fixture),
            final_snapshot=copy.deepcopy(self.state),
            trace=list(self.events),
            final_response=response,
        )


def insert(collection: str, record_id: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"collection": collection, "record_id": record_id, "operation": "insert", "data": data}


def by_dimension(scores: list[Score]) -> dict[str, Score]:
    return {s.dimension: s for s in scores}


# The full refund of ord_1001, done right. Most tests change one thing in it.
REFUND_SPEC: dict[str, Any] = {
    "expected_refunds": [{"order_id": "ord_1001", "amount_minor": 24999}],
    "required_calls": [
        {"tool": "get_order", "args": {"order_id": "ord_1001"}},
        {"tool": "get_return_status", "args": {"order_id": "ord_1001"}},
    ],
}


def correct_refund(trace: Trace | None = None) -> Trace:
    trace = trace or Trace()
    trace.call("get_order", {"order_id": "ord_1001"})
    trace.call("get_return_status", {"order_id": "ord_1001"})
    approval = trace.approve("ord_1001", 24999)
    trace.refund("ord_1001", 24999, approval, number=2)
    return trace
