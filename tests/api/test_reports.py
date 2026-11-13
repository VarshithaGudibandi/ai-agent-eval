from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI

from app.explain import job as explain_job_module
from app.settings import Settings
from tests.api.conftest import Caller, SignIn, score_now
from tests.api.seeding import V2, add_dataset, run_experiment
from tests.explain.scripted_auditor import ScriptedAuditor
from tests.ingest.sample_traces import CONFIG, support_trace

CAUSE = "The new prompt dropped the instruction to look the order up first."


def _use(monkeypatch: pytest.MonkeyPatch, auditor: ScriptedAuditor) -> None:
    @asynccontextmanager
    async def provider(api_key: str, _: Settings) -> AsyncIterator[ScriptedAuditor]:
        yield auditor

    monkeypatch.setattr(explain_job_module, "judge_provider", provider)


async def _held(owner: Caller, app: FastAPI, key: bool = True) -> str:
    if key:
        await owner.post(
            "/api/v1/provider-credentials",
            json={"provider": "gemini", "label": "free", "api_key": "AIza" + "E" * 35},
        )
    dataset_id = await add_dataset(owner)
    v1 = await run_experiment(owner, app, dataset_id, CONFIG, crash=set())
    v2 = await run_experiment(owner, app, dataset_id, V2, crash=set(range(6)))
    body = {"baseline": {"experiment_id": v1["id"]}, "candidate": {"experiment_id": v2["id"]}}
    comparison = (await owner.post("/api/v1/comparisons", json=body)).json()
    assert comparison["result"]["decision"] == "hold"
    return str(comparison["id"])


async def _report(owner: Caller, app: FastAPI, comparison_id: str) -> dict[str, Any]:
    queued = await owner.post("/api/v1/reports", json={"comparison_id": comparison_id})
    assert queued.status_code == 202 and queued.json()["status"] == "queued"
    await score_now(app, owner.workspace_id)
    done = (await owner.get(f"/api/v1/reports/{queued.json()['id']}")).json()
    assert done["status"] == "done", done
    report: dict[str, Any] = done["report"]
    return report


async def test_a_report_names_the_change_with_cited_spans(
    new_owner: SignIn, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    auditor = ScriptedAuditor(CAUSE)
    _use(monkeypatch, auditor)
    owner = await new_owner()
    report = await _report(owner, app, await _held(owner, app))
    [change] = report["changes"]
    assert change["field"] == "prompt"
    [group] = report["groups"]
    assert (group["key"], group["count"], group["baseline_count"]) == ("run_completed", 12, 0)
    # One change, paired cases, and the cause shown in all five runs read: high.
    assert (group["cause"], group["confidence"]) == (CAUSE, "high")
    cited = {s["run_id"] for c in group["claims"] for s in c["spans"]}
    assert cited == set(group["example_runs"]) and len(cited) == 5
    assert report["headline"].startswith("run_completed now fails in 12 runs")
    assert CAUSE in report["headline"]
    assert report["next_step"] == "Restore the removed sentence and run the eval again."
    # The auditor read the diff and the failing runs, fenced as data.
    prompt = auditor.prompts[0][1].content
    assert "A shorter prompt that forgets to look things up." in prompt and "<<<DATA" in prompt


async def test_invented_evidence_leaves_no_cause(
    new_owner: SignIn, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, ScriptedAuditor(CAUSE, invent=True))
    owner = await new_owner()
    report = await _report(owner, app, await _held(owner, app))
    [group] = report["groups"]
    assert (group["cause"], group["confidence"], group["claims"]) == (
        "no cause the evidence supports",
        "low",
        [],
    )
    assert CAUSE not in report["headline"]


async def test_without_a_key_the_findings_are_kept_and_the_cause_says_why(
    new_owner: SignIn, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, ScriptedAuditor(CAUSE))
    owner = await new_owner()
    report = await _report(owner, app, await _held(owner, app, key=False))
    [group] = report["groups"]
    assert group["cause"] is None and "no Gemini key" in group["error"]
    assert report["changes"][0]["field"] == "prompt" and group["count"] == 12


def _dated(trace_id: str, day: str, crash: bool) -> dict[str, Any]:
    trace = support_trace(trace_id)
    for span in trace["spans"]:
        span["start"] = span["start"].replace("2026-11-12", day)
        span["end"] = span["end"].replace("2026-11-12", day)
    if crash:
        trace["spans"][0]["error"] = "RuntimeError: broke"
    return trace


async def test_a_live_report_needs_a_paid_key(
    new_owner: SignIn, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, ScriptedAuditor(CAUSE))
    owner = await new_owner()
    await owner.post(
        "/api/v1/provider-credentials",
        json={"provider": "gemini", "label": "free", "api_key": "AIza" + "F" * 35},
    )
    traces = [_dated(f"old-{n}", "2026-11-05", False) for n in range(3)]
    traces += [_dated(f"new-{n}", "2026-11-12", True) for n in range(3)]
    await owner.post("/api/v1/traces", json={"traces": traces})
    await score_now(app, owner.workspace_id)
    [agent] = (await owner.get("/api/v1/agents")).json()
    body = {
        "baseline": {
            "agent_id": agent["id"],
            "start": "2026-11-01T00:00:00Z",
            "end": "2026-11-10T00:00:00Z",
        },
        "candidate": {
            "agent_id": agent["id"],
            "start": "2026-11-10T00:00:00Z",
            "end": "2026-11-20T00:00:00Z",
        },
    }
    comparison = (await owner.post("/api/v1/comparisons", json=body)).json()
    report = await _report(owner, app, comparison["id"])
    [group] = report["groups"]
    assert group["count"] == 3 and "marked paid" in group["error"]
    assert any("not paired" in limit for limit in report["limits"])
