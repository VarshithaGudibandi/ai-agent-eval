import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.agents.contract import RunLimits
from app.agents.http import (
    MAX_USAGE_KEYS,
    ExternalAgentUnavailableError,
    HttpAgentAdapter,
    HttpAgentSettings,
    _counts,
    handshake,
)
from app.agents.http_guard import GuardPolicy
from app.agents.signing import SIGNATURE_HEADER, verify
from app.environments.contract import AgentTaskInput
from app.gateway.contract import ToolCallOutcome
from app.providers.contract import PublicToolSchema

POLICY = GuardPolicy(allow_local=True, require_https=False, max_response_bytes=10_000)
TASK = AgentTaskInput(user_message="Refund ord_1", context={"authenticated_customer_id": "c1"})
SECRET = "whsec_test"


class Link:
    attempt_id = "att_1"
    gateway_url = "http://gateway.test"
    signing_secret = SECRET
    policy = POLICY

    def __init__(self, posted: dict[str, Any] | None = None) -> None:
        self.posted = posted
        self.tokens: list[float] = []

    async def run_token(self, seconds: float) -> str:
        self.tokens.append(seconds)
        return "run_secret"

    async def posted_result(self) -> dict[str, Any] | None:
        return self.posted


class Tools:
    @property
    def schemas(self) -> list[PublicToolSchema]:
        return [PublicToolSchema(name="get_order", description="d", parameters={})]

    async def call(self, tool: str, arguments: dict[str, Any]) -> ToolCallOutcome:
        raise AssertionError("an HTTP agent calls the gateway itself")


class Events:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, dict[str, Any]]] = []

    async def record(self, event_type: str, payload: dict[str, Any]) -> str:
        self.recorded.append((event_type, payload))
        return "evt"


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response], link: Link, url: str = "http://127.0.0.1/a"
) -> HttpAgentAdapter:
    return HttpAgentAdapter(
        HttpAgentSettings(url=url),
        link,
        poll_seconds=0.01,
        client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_a_signed_task_goes_out_and_the_reply_comes_back() -> None:
    seen: list[httpx.Request] = []

    def agent(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"final_response": "Done.", "usage": {"tokens": 5}})

    events = Events()
    link = Link()
    result = await _adapter(agent, link).run(TASK, Tools(), events, RunLimits(timeout_seconds=30))
    assert (result.final_response, result.stop_reason) == ("Done.", "model_final")
    assert result.metadata == {"usage_self_reported": {"tokens": 5}}
    [request] = seen
    assert verify(SECRET, request.headers[SIGNATURE_HEADER], request.content)
    body = json.loads(request.content)
    assert body["tool_gateway"] == {
        "url": "http://gateway.test/gw/v1/attempts/att_1/tools",
        "token": "run_secret",
    }
    assert (body["kind"], body["task"]["user_message"], body["tools"][0]["name"]) == (
        "task",
        "Refund ord_1",
        "get_order",
    )
    assert link.tokens == [30]
    # The trace notes the reply, but never the run token.
    assert events.recorded[0][0] == "external_agent_replied"
    assert "run_secret" not in str(events.recorded)


async def test_an_agent_can_answer_later_through_the_callback() -> None:
    def accepted(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202)

    later = Link(posted={"final_response": "Refunded.", "usage": None})
    result = await _adapter(accepted, later).run(TASK, Tools(), Events(), RunLimits())
    assert (result.final_response, result.stop_reason) == ("Refunded.", "model_final")
    never = await _adapter(accepted, Link()).run(
        TASK, Tools(), Events(), RunLimits(timeout_seconds=0.1)
    )
    assert never.stop_reason == "timeout"


@pytest.mark.parametrize(
    ("status", "body", "stop_reason"),
    [(400, b"{}", "agent_error"), (200, b"not json", "agent_error"), (200, b"{}", "agent_error")],
)
async def test_bad_replies_are_the_agents_fault(status: int, body: bytes, stop_reason: str) -> None:
    def agent(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)

    result = await _adapter(agent, Link()).run(TASK, Tools(), Events(), RunLimits())
    assert result.stop_reason == stop_reason


async def test_a_down_or_blocked_endpoint_is_unavailable_not_a_failure() -> None:
    attempts = 0

    def refused(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("refused", request=request)

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(ExternalAgentUnavailableError):
        await _adapter(refused, Link()).run(TASK, Tools(), Events(), RunLimits())
    assert attempts == 3
    with pytest.raises(ExternalAgentUnavailableError):
        await _adapter(broken, Link()).run(TASK, Tools(), Events(), RunLimits())
    strict = Link()
    strict.policy = GuardPolicy(allow_local=False, require_https=False, max_response_bytes=1)
    with pytest.raises(ExternalAgentUnavailableError, match="blocked"):
        await _adapter(broken, strict).run(TASK, Tools(), Events(), RunLimits())


async def test_handshake_reports_what_is_wrong() -> None:
    def good(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["kind"] == "handshake"
        return httpx.Response(200, json={"protocol_version": "1"})

    def old(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hello": "there"})

    def client(handler: Callable[[httpx.Request], httpx.Response]) -> Any:
        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ok = await handshake("http://127.0.0.1/a", SECRET, POLICY, client=client(good))
    wrong = await handshake("http://127.0.0.1/a", SECRET, POLICY, client=client(old))
    blocked = await handshake("http://169.254.169.254/", SECRET, POLICY, client=client(good))
    assert ok.ok and ok.status_code == 200
    assert not wrong.ok and "protocol_version" in (wrong.problem or "")
    assert not blocked.ok and (blocked.problem or "").startswith("blocked")


def test_only_flat_counts_of_reported_usage_are_kept() -> None:
    reported = {
        "input_tokens": 10,
        "cost": 0.5,
        "nested": {"a": 1},
        "negative": -1,
        "flag": True,
        "x" * 65: 1,
        **{f"k{i:02}": i for i in range(30)},
    }
    kept = _counts(reported)
    assert kept is not None and len(kept) == MAX_USAGE_KEYS
    assert set(kept) <= {"input_tokens"} | {f"k{i:02}" for i in range(30)}
    assert _counts({"nested": {"a": 1}}) is None
    assert _counts(None) is None
