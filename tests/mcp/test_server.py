import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client, StdioServerParameters

from agent_eval_mcp.server import server
from tests.api.seeding import at
from tests.ingest.sample_traces import CONFIG, support_trace
from tests.mcp.conftest import SPAN_INPUT, SPAN_OUTPUT, V2, Workspace, run_experiment


async def _call(tool: str, **arguments: Any) -> tuple[bool, Any, str]:
    async with Client(server) as client:
        result = await client.call_tool(tool, arguments)
    text = " ".join(c.text for c in result.content if hasattr(c, "text"))
    return result.is_error, result.structured_content, text


async def test_the_server_offers_its_read_tools() -> None:
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert {"list_agents", "agent_health", "show_failing_runs"} <= set(tools)
    # The assistant reads these: they say what the answer is about and that it is recorded.
    assert "how old it is" in (tools["agent_health"].description or "")


async def test_reads_answer_from_the_recorded_runs_and_never_quote_them(use: Workspace) -> None:
    error, found, _ = await _call("list_agents")
    assert not error and found["agents"] == []
    run_experiment(use, CONFIG, crash=set())
    run_experiment(use, V2, crash={0, 1, 2, 3})
    error, agents, _ = await _call("list_agents")
    assert not error and [a["name"] for a in agents["agents"]] == ["support-bot"]
    error, health, _ = await _call("agent_health", agent="support-bot")
    assert not error and health["mode"] == "versions"
    success = next(m for m in health["metrics"] if m["name"] == "success rate")
    assert (success["now"], success["before"], success["change"], success["verdict"]) == (
        "50.0%",
        "100.0%",
        "-50.0 pts",
        "real",
    )
    assert "minute" in health["about"] or "just now" in health["about"]
    assert any("evalctl run" in note for note in health["notes"])
    error, failing, _ = await _call("show_failing_runs", agent="support-bot")
    assert not error and (failing["failed"], failing["runs"]) == (8, 16)
    [reason] = failing["reasons"]
    assert reason["scorer"] == "run_completed" and "forgot the lookup" in reason["reason"]
    assert reason["example_runs"][0].startswith(f"{use.url}/runs/")
    everything = json.dumps([agents, health, failing])
    assert SPAN_INPUT not in everything and SPAN_OUTPUT not in everything
    assert use.member not in everything


async def test_an_unknown_agent_names_the_real_ones(use: Workspace) -> None:
    run_experiment(use, CONFIG, crash=set())
    error, _, text = await _call("agent_health", agent="suport-bot")
    assert error and "No agent named 'suport-bot'" in text and "support-bot" in text


async def test_a_viewer_token_can_read(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_EVAL_URL", workspace.url)
    monkeypatch.setenv("AGENT_EVAL_TOKEN", workspace.viewer)
    error, found, _ = await _call("list_agents")
    assert not error and found["agents"] == []


async def test_a_missing_or_wrong_token_or_no_platform_says_what_to_do(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_EVAL_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_EVAL_URL", workspace.url)
    error, _, text = await _call("list_agents")
    assert error and "Set AGENT_EVAL_TOKEN" in text
    wrong = "aet_" + "x" * 40
    monkeypatch.setenv("AGENT_EVAL_TOKEN", wrong)
    error, _, text = await _call("list_agents")
    assert error and "refused the token" in text and wrong not in text
    monkeypatch.setenv("AGENT_EVAL_URL", "http://127.0.0.1:9")
    error, _, text = await _call("list_agents")
    assert error and "Could not reach the platform" in text and wrong not in text


async def test_did_my_last_change_break_anything(use: Workspace) -> None:
    # The acceptance question: compare_versions answers it, explain_regression adds the report.
    run_experiment(use, CONFIG, crash=set())
    run_experiment(use, V2, crash={0, 1, 2, 3})
    error, first, _ = await _call("compare_versions", agent="support-bot")
    assert not error and first["decision"] == "hold"
    assert first["summary"].startswith("HOLD: new hard failures: run_completed")
    assert [c["case"] for c in first["worse_cases"]] == ["case-0", "case-1", "case-2", "case-3"]
    assert (
        first["report"] is None and "Ask explain_regression for why it got worse." in first["notes"]
    )
    assert "on version v1" in first["baseline"] and "on version v1" in first["candidate"]
    error, explained, _ = await _call("explain_regression", agent="support-bot")
    assert not error and explained["report"]["status"] == "done"
    assert explained["summary"].startswith("run_completed now fails in 8 runs")
    [cause] = explained["report"]["causes"]
    assert cause["no_cause"].startswith("no Gemini key")
    error, again, _ = await _call("compare_versions", agent="support-bot")
    assert not error and again["page"] == first["page"]  # the stored comparison is reused
    assert again["report"]["id"] == explained["report"]["id"]
    assert "Report: run_completed now fails in 8 runs" in again["summary"]
    everything = json.dumps([first, explained, again])
    assert SPAN_INPUT not in everything and SPAN_OUTPUT not in everything
    assert use.member not in everything


async def test_a_report_is_asked_for_once(use: Workspace) -> None:
    run_experiment(use, CONFIG, crash=set())
    run_experiment(use, V2, crash={0})
    _, one, _ = await _call("explain_regression", agent="support-bot")
    _, two, _ = await _call("explain_regression", agent="support-bot")
    assert one["report"]["id"] == two["report"]["id"]
    comparison_id = one["page"].rsplit("/", 1)[1]
    async with httpx.AsyncClient(
        base_url=use.url, headers={"authorization": f"Bearer {use.member}"}
    ) as client:
        reports = (await client.get(f"/api/v1/reports?comparison_id={comparison_id}")).json()
    assert len(reports) == 1


async def test_a_viewer_reads_comparisons_but_cannot_make_them_or_spend(
    use: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_experiment(use, CONFIG, crash=set())
    run_experiment(use, V2, crash={0})
    monkeypatch.setenv("AGENT_EVAL_TOKEN", use.viewer)
    error, _, text = await _call("compare_versions", agent="support-bot")
    assert error and "needs a member token" in text
    monkeypatch.setenv("AGENT_EVAL_TOKEN", use.member)
    await _call("compare_versions", agent="support-bot")
    monkeypatch.setenv("AGENT_EVAL_TOKEN", use.viewer)
    error, found, _ = await _call("compare_versions", agent="support-bot")
    assert not error and found["decision"] == "hold"
    error, _, text = await _call("explain_regression", agent="support-bot")
    assert error and "needs a member token" in text


async def test_one_version_has_nothing_to_compare(use: Workspace) -> None:
    run_experiment(use, CONFIG, crash=set())
    error, _, text = await _call("compare_versions", agent="support-bot")
    assert error and "Only one version of support-bot has run on this dataset" in text


async def test_live_only_agents_compare_this_week_with_the_last(use: Workspace) -> None:
    now = datetime.now(UTC)
    traces = [
        at(support_trace(f"{week}-{n}"), now - timedelta(days=days, minutes=n))
        for week, days in (("prev", 10), ("last", 3))
        for n in range(3)
    ]
    async with httpx.AsyncClient(
        base_url=use.url, headers={"authorization": f"Bearer {use.member}"}
    ) as client:
        await client.post("/api/v1/traces", json={"traces": traces})
        for _ in range(100):
            agents = (await client.get("/api/v1/agents")).json()
            if agents:
                health = (await client.get(f"/api/v1/agents/{agents[0]['id']}/health")).json()
                if health["mode"] == "live":
                    break
            await asyncio.sleep(0.3)
    error, found, _ = await _call("compare_versions", agent="support-bot")
    assert not error and found["baseline"] == "live runs of the week before"
    assert found["decision"] == "insufficient_data" and found["worse_cases"] == []


async def test_the_installed_server_speaks_mcp_over_stdio(
    use: Workspace, capfd: pytest.CaptureFixture[str]
) -> None:
    script = Path(sys.executable).parent / "agent-eval-mcp"
    params = StdioServerParameters(
        command=str(script),
        env={"AGENT_EVAL_URL": use.url, "AGENT_EVAL_TOKEN": use.member, "PATH": os.environ["PATH"]},
    )
    async with Client(params) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("list_agents", {})
        failed = await client.call_tool("agent_health", {"agent": "nobody"})
    assert tools == {
        "list_agents",
        "agent_health",
        "show_failing_runs",
        "compare_versions",
        "explain_regression",
    }
    assert not result.is_error and result.structured_content["agents"] == []
    assert failed.is_error
    # The server's own log, which a failed call writes to, never holds the token.
    assert use.member not in capfd.readouterr().err
