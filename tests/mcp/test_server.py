import json
from typing import Any

import pytest
from agent_eval_mcp.server import server

from mcp import Client
from tests.ingest.sample_traces import CONFIG
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
