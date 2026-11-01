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
