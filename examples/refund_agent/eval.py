"""Run the refund agent on its dataset, three times per case.

Needs google-genai, GEMINI_API_KEY, and AGENT_EVAL_URL and AGENT_EVAL_TOKEN for the platform.
From the repository root: uv run --with google-genai python -m examples.refund_agent.eval
"""

from pathlib import Path

from agent_eval import Eval
from examples.refund_agent.agent import CONFIG, RefundAgent

DATASET = Path(__file__).with_name("dataset.jsonl")
# How a run of this agent is graded: two built-in checks and two rules of its own.
SCORERS = [
    {"key": "run_completed", "check": "run_completed"},
    {"key": "valid_tool_arguments", "check": "valid_tool_arguments"},
    {
        "key": "refund_after_lookup",
        "check": "tool_order",
        "params": {"tool": "issue_refund", "after": ["get_order"]},
    },
    {
        "key": "refund_amount",
        "check": "matches_expected",
        "params": {"field": "refund", "tool": "issue_refund", "argument": "amount"},
    },
]


def main() -> None:
    # Imported here so the agent and its tests need no Gemini package or key.
    from google import genai

    agent = RefundAgent(genai.Client())
    Eval(
        agent="refund-agent",
        task=agent.handle,
        dataset=DATASET,
        config=CONFIG,
        repetitions=3,
        label="v1",
        scorers=SCORERS,
    )


if __name__ == "__main__":
    main()
