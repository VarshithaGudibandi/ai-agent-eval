from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.environments.contract import TaskVersion

Layer = Literal["outcome", "policy", "trajectory", "response"]
ScoreStatus = Literal["pass", "fail", "partial", "not_applicable", "evaluation_error"]


class TraceEvent(BaseModel):
    id: str
    sequence_number: int
    event_type: str
    payload: dict[str, Any]


class ToolCallView(BaseModel):
    """One tool call as the trace recorded it: what was asked and what really happened."""

    tool_call_id: str
    tool: str
    arguments: dict[str, Any]
    request_event_id: str
    result_event_id: str | None = None
    # ok, error or rejected. A call with no result event never finished.
    status: str = "error"
    error_code: str | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    state_changes: list[dict[str, Any]] = Field(default_factory=list)
    fault: dict[str, Any] | None = None
    replayed: bool = False

    @property
    def event_id(self) -> str:
        return self.result_event_id or self.request_event_id

    def inserted(self, collection: str) -> list[dict[str, Any]]:
        """Records this call really created, even if a fault hid them from the agent."""
        return [
            c["data"]
            for c in self.state_changes
            if c["collection"] == collection and c["operation"] == "insert"
        ]


class EvalContext(BaseModel):
    """Everything an evaluator may read. The agent never saw the fixture or the spec."""

    task: TaskVersion
    final_snapshot: dict[str, Any]
    trace: list[TraceEvent]
    final_response: str

    def tool_result_event(self, tool_call_id: str) -> TraceEvent | None:
        for event in self.trace:
            if (
                event.event_type == "tool_call_completed"
                and event.payload.get("tool_call_id") == tool_call_id
            ):
                return event
        return None

    def tool_calls(self) -> list[ToolCallView]:
        """Every tool call in the order it was asked for."""
        calls: dict[str, ToolCallView] = {}
        for event in self.trace:
            payload = event.payload
            if event.event_type == "tool_call_requested":
                calls[payload["tool_call_id"]] = ToolCallView(
                    tool_call_id=payload["tool_call_id"],
                    tool=payload["tool"],
                    arguments=payload["arguments"],
                    request_event_id=event.id,
                )
            elif event.event_type == "tool_call_completed":
                call = calls.get(payload["tool_call_id"])
                if call is None:
                    continue
                call.result_event_id = event.id
                call.status = payload["status"]
                call.error_code = payload.get("error_code")
                call.output = payload.get("output") or {}
                call.state_changes = payload.get("state_changes") or []
                call.fault = payload.get("fault")
                call.replayed = bool(payload.get("replayed"))
        return list(calls.values())

    def finish_event_id(self) -> str | None:
        """Where the agent stopped, for failures that have no single call to point at."""
        finished = [e.id for e in self.trace if e.event_type == "agent_finished"]
        if finished:
            return finished[-1]
        return self.trace[-1].id if self.trace else None


class Score(BaseModel):
    """One evaluator's verdict on one dimension. A failure must point at its evidence."""

    evaluator: str
    version: str
    layer: Layer
    dimension: str
    applicable: bool
    status: ScoreStatus
    score: float | None
    reason_code: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class Evaluator(Protocol):
    """Grades one attempt on one or more dimensions, from state the agent never saw."""

    name: str
    version: str
    layer: Layer

    def applies_to(self, task: TaskVersion) -> bool:
        """Whether this evaluator has anything to say about the task."""
        ...

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        """Return one score per dimension."""
        ...
