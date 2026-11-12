import os

import httpx

from app.providers.contract import GenerationSettings, Message, ModelResponse
from app.providers.gemini import GeminiProvider
from app.scoring.judges import (
    DEFAULT_JUDGE_MODEL,
    ChecklistParams,
    ClaimCheckParams,
    Question,
    run_checklist,
    run_claim_check,
)
from tests.scoring.golden import golden, view

MODEL = os.environ.get("GEMINI_JUDGE_MODEL", DEFAULT_JUDGE_MODEL)
QUESTIONS = [
    Question(id="looked_up", text="Did the agent look the order up before answering?"),
    Question(id="refunded", text="Does the reply say a refund was made?"),
]


async def test_a_real_judge_answers_with_quotes_from_the_run() -> None:
    settings = GenerationSettings(
        model=MODEL, temperature=0, max_output_tokens=2048, json_output=True
    )
    async with httpx.AsyncClient() as client:
        provider = GeminiProvider(os.environ["GEMINI_API_KEY"], client)

        async def ask(messages: list[Message]) -> ModelResponse:
            return await provider.generate(messages, [], settings, timeout=60)

        run = view(golden("refund"), "refund")
        checklist = await run_checklist(run, ChecklistParams(questions=QUESTIONS), 0.7, ask)
        claims = await run_claim_check(run, ClaimCheckParams(), ask)
    # A clear run: the judge should answer both, each with a quote found in the run.
    assert [a.answer for a in checklist.answers] == ["yes", "yes"]
    assert all(a.evidence for a in checklist.answers)
    assert claims.claims and all(c.verdict == "supported" for c in claims.claims)
