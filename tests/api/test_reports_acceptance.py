"""Phase 6 acceptance: a planted prompt change that drops the lookup step yields a report
naming it, with high confidence, citing spans that really exist."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI

from agent_eval.evaluation import load_cases
from agent_eval.recording import (
    RunContext,
    config_document,
    reset_eval_context,
    use_eval_context,
)
from app.explain import job as explain_job_module
from app.settings import Settings
from examples.refund_agent.agent import CONFIG, PROMPT, RefundAgent
from examples.refund_agent.eval import DATASET, SCORERS
from tests.api.conftest import Caller, SignIn, score_now
from tests.examples.test_refund_agent import scripted_gemini
from tests.explain.scripted_auditor import ScriptedAuditor
from tests.sdk.fake_gemini import FakeGemini, Response, reply

LOOKUP = "Always look up the order before you answer."
V2 = {**CONFIG, "prompt": PROMPT.replace(f" {LOOKUP}", "")}
CAUSE = "v2's prompt no longer says to look the order up, so refunds go out unchecked."


def careless(model: str, contents: list[Any], config: Any) -> Response:
    """v2's behaviour: without the lookup instruction it refunds straight away."""
    message = contents[0]["parts"][0]["text"]
    if "refund" in message.lower() and "ord_" in message and len(contents) == 1:
        order = message[message.index("ord_") :][:8]
        return reply(None, ("issue_refund", {"order_id": order, "amount": 59.0}))
    return scripted_gemini(model, contents, config)


async def _experiment(
    owner: Caller, app: FastAPI, dataset_id: str, config: dict[str, Any], brain: Any
) -> str:
    """The example agent run on every case twice, recorded by the SDK, sent over the API."""
    started = (
        await owner.post(
            "/api/v1/experiments",
            json={
                "agent": "refund-agent",
                "config": config_document(config),
                "dataset_version_id": dataset_id,
                "repetitions": 2,
            },
        )
    ).json()
    agent = RefundAgent(FakeGemini(brain))
    traces: list[dict[str, Any]] = []
    for case in started["cases"]:
        for rep in (1, 2):
            tags = {"experiment": started["id"], "case": case["key"], "repetition": rep}
            token = use_eval_context(RunContext("refund-agent", config, None, tags, traces.append))
            try:
                agent.handle(case["input"])
            finally:
                reset_eval_context(token)
    sent = await owner.post("/api/v1/traces", json={"traces": traces})
    assert sent.json()["rejected"] == []
    await owner.post(f"/api/v1/experiments/{started['id']}/finish")
    await score_now(app, owner.workspace_id)
    return str(started["id"])


async def test_dropping_the_lookup_step_is_named_with_high_confidence(
    new_owner: SignIn, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    auditor = ScriptedAuditor(CAUSE)

    @asynccontextmanager
    async def provider(api_key: str, _: Settings) -> AsyncIterator[ScriptedAuditor]:
        yield auditor

    monkeypatch.setattr(explain_job_module, "judge_provider", provider)
    owner = await new_owner()
    await owner.post(
        "/api/v1/provider-credentials",
        json={"provider": "gemini", "label": "free", "api_key": "AIza" + "Q" * 35},
    )
    dataset = await owner.post(
        "/api/v1/datasets", json={"name": "refunds", "cases": load_cases(DATASET)}
    )
    dataset_id = dataset.json()["id"]
    # A first run makes the agent, so its own scorers can be saved before the runs compared.
    await _experiment(owner, app, dataset_id, CONFIG, scripted_gemini)
    [agent] = (await owner.get("/api/v1/agents")).json()
    await owner.put(f"/api/v1/agents/{agent['id']}/eval-spec", json={"scorers": SCORERS})
    v1 = await _experiment(owner, app, dataset_id, CONFIG, scripted_gemini)
    v2 = await _experiment(owner, app, dataset_id, V2, careless)
    body = {"baseline": {"experiment_id": v1}, "candidate": {"experiment_id": v2}}
    comparison = (await owner.post("/api/v1/comparisons", json=body)).json()
    assert comparison["result"]["decision"] == "hold"
    queued = (await owner.post("/api/v1/reports", json={"comparison_id": comparison["id"]})).json()
    await score_now(app, owner.workspace_id)
    report = (await owner.get(f"/api/v1/reports/{queued['id']}")).json()["report"]
    # The change: one prompt sentence removed, and nothing else.
    [change] = report["changes"]
    assert change["field"] == "prompt" and change["detail"] == [f"-{LOOKUP}"]
    top = report["groups"][0]
    assert top["scorer_key"] == "refund_after_lookup"
    assert (top["cause"], top["confidence"]) == (CAUSE, "high")
    assert CAUSE in report["headline"]
    # Every cited span is a real span of the run it names.
    for claim in top["claims"]:
        for ref in claim["spans"]:
            run = (await owner.get(f"/api/v1/runs/{ref['run_id']}")).json()
            assert ref["span_id"] in {s["span_id"] for s in run["spans"]}
    assert top["claims"] and len({s["run_id"] for c in top["claims"] for s in c["spans"]}) >= 3
