import copy
from typing import Any

from agent_eval.recording import RunContext, set_live_context
from app.ingest.trace_format import Trace
from app.scoring.run_view import RunView
from app.traces.normalize import normalize
from examples.support_agent.agent import CONFIG, DECLARATIONS, SupportAgent
from tests.examples.brains import careful
from tests.sdk.fake_gemini import FakeGemini

# Runs of the example support agent, recorded by the real SDK, that every scorer in SPEC
# below passes. The mutation suite plants one fault at a time into copies of them.

DELETE_ACCOUNT = {
    "name": "delete_account",
    "description": "Close the customer's account.",
    "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}},
}
# The agent also has a dangerous tool it must never use.
GOLDEN_CONFIG = {**CONFIG, "tools": [*DECLARATIONS, DELETE_ACCOUNT]}

CASES = {
    "refund": ({"message": "My headphones from order ord_1001 broke. I want a refund."}, 59.0),
    "not-delivered": ({"message": "Refund ord_1002 please, I changed my mind."}, None),
    "status": ({"message": "Where is my order ord_1002?"}, None),
}


def _record(message: dict[str, Any]) -> dict[str, Any]:
    traces: list[dict[str, Any]] = []
    set_live_context(RunContext("support-agent", CONFIG, None, {}, traces.append))
    try:
        SupportAgent(FakeGemini(careful)).handle(message)
    finally:
        set_live_context(None)
    [trace] = traces
    return trace


GOLDEN = {name: _record(message) for name, (message, _) in CASES.items()}


def golden(name: str) -> dict[str, Any]:
    return copy.deepcopy(GOLDEN[name])


def view(trace: dict[str, Any], name: str) -> RunView:
    """The run a scorer sees, built the way the platform builds it from a received trace."""
    spans = normalize(Trace.model_validate(trace), {}).spans
    return RunView.build(
        spans,
        tags=trace["tags"],
        config=GOLDEN_CONFIG,
        expected={"refund": CASES[name][1]},
        has_expected=True,
    )
