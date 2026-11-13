import os
from typing import Any

import httpx

from agent_eval.recording import RunContext, set_live_context
from app.explain.auditor import ShownRun, messages, read_reply
from app.explain.diff import config_diff
from app.explain.groups import FailureGroup
from app.ingest.trace_format import Trace
from app.providers.contract import GenerationSettings
from app.providers.gemini import GeminiProvider
from app.scoring.judge_prompts import Transcript
from app.scoring.run_view import RunView
from app.traces.normalize import normalize
from examples.refund_agent.agent import CONFIG, RefundAgent
from tests.api.test_reports_acceptance import V2, careless
from tests.sdk.fake_gemini import FakeGemini

MODEL = os.environ.get("EXPLAINER_MODEL", "gemini-3.8-flash")


def _careless_run(message: str) -> RunView:
    traces: list[dict[str, Any]] = []
    set_live_context(RunContext("refund-agent", V2, None, {}, traces.append))
    try:
        RefundAgent(FakeGemini(careless)).handle({"message": message})
    finally:
        set_live_context(None)
    [trace] = traces
    return RunView.build(normalize(Trace.model_validate(trace), {}).spans, {}, V2)


async def test_a_real_auditor_finds_the_dropped_lookup_with_cited_spans() -> None:
    runs = [
        ShownRun(f"r{n}", f"run{n}", Transcript.of(_careless_run(message)))
        for n, message in enumerate(
            [
                "My headphones from order ord_1001 broke. I want a refund.",
                "Please refund order ord_1003, the backpack ripped.",
                "Refund ord_1002 please, I changed my mind.",
            ],
            start=1,
        )
    ]
    changes = config_diff(CONFIG, V2)
    group = FailureGroup(
        "refund_after_lookup",
        "refund_after_lookup",
        None,
        ("run1", "run2", "run3"),
        0,
        ("issue_refund was called before get_order had finished",),
    )
    settings = GenerationSettings(
        model=MODEL, temperature=0, max_output_tokens=4096, json_output=True
    )
    async with httpx.AsyncClient() as client:
        provider = GeminiProvider(os.environ["GEMINI_API_KEY"], client)
        response = await provider.generate(
            messages(changes, group, runs), [], settings, timeout=120
        )
    audit = read_reply(response.text, runs, changes, True, response.resolved_model)
    print(audit)
    # The claims that survived cite real spans, and the cause is about the lookup.
    assert audit.claims, "the auditor cited no span it was shown"
    assert "look" in audit.cause.lower() or "get_order" in audit.cause
