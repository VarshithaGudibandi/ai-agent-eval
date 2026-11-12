"""Run the refund agent on its dataset, three times per case.

Needs google-genai, GEMINI_API_KEY, and AGENT_EVAL_URL and AGENT_EVAL_TOKEN for the platform.
From the repository root: uv run --with google-genai python -m examples.refund_agent.eval
"""

from pathlib import Path

from agent_eval import Eval
from examples.refund_agent.agent import CONFIG, RefundAgent

DATASET = Path(__file__).with_name("dataset.jsonl")


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
    )


if __name__ == "__main__":
    main()
