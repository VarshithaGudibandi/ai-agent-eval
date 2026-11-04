import asyncio
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agents.contract import AgentResult, EventRecorder, RunLimits
from app.agents.http_guard import EndpointBlockedError, GuardPolicy, ResponseTooLargeError, post
from app.agents.signing import SIGNATURE_HEADER, sign
from app.environments.contract import AgentTaskInput
from app.gateway.contract import ToolSession

PROTOCOL_VERSION = "1"
# Connection failures are retried this many times before the endpoint counts as down. A
# request that reached the agent is never resent, since the agent may already have acted.
CONNECT_RETRIES = 2


class HttpAgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2000)

    @field_validator("url")
    @classmethod
    def _http_url(cls, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("url must be http or https with a host")
        return url


class ExternalAgentUnavailableError(Exception):
    """The endpoint could not be reached or answered with a server error.

    That says nothing about the agent's decisions, so the run is left unscored.
    """


class AgentReply(BaseModel):
    final_response: str = Field(max_length=100_000)
    usage: dict[str, Any] | None = None


class ExternalLink(Protocol):
    """What the adapter needs from the platform to hand one attempt to an external agent."""

    attempt_id: str
    gateway_url: str
    signing_secret: str
    policy: GuardPolicy

    async def run_token(self, seconds: float) -> str:
        """Issue the attempt's run token, valid for at most this long."""
        ...

    async def posted_result(self) -> dict[str, Any] | None:
        """The answer the agent posted back, if it replied 202."""
        ...


def signed_headers(secret: str, body: bytes, attempt_id: str) -> dict[str, str]:
    return {SIGNATURE_HEADER: sign(secret, body), "x-agent-eval-attempt": attempt_id}


class HttpAgentAdapter:
    """Sends one signed request to a team's agent, which acts only through the gateway."""

    def __init__(
        self,
        settings: HttpAgentSettings,
        link: ExternalLink,
        poll_seconds: float = 0.5,
        client: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(trust_env=False),
    ) -> None:
        self._settings = settings
        self._link = link
        self._poll_seconds = poll_seconds
        # trust_env is off by default so no proxy sits between the guard's checked address
        # and the agent.
        self._client = client

    async def run(
        self,
        task_input: AgentTaskInput,
        tools: ToolSession,
        events: EventRecorder,
        limits: RunLimits,
    ) -> AgentResult:
        started = time.monotonic()
        token = await self._link.run_token(limits.timeout_seconds)
        deadline = datetime.now(UTC) + timedelta(seconds=limits.timeout_seconds)
        base = f"{self._link.gateway_url.rstrip('/')}/gw/v1/attempts/{self._link.attempt_id}"
        body = json.dumps(
            {
                "protocol_version": PROTOCOL_VERSION,
                "kind": "task",
                "attempt_id": self._link.attempt_id,
                "task": task_input.model_dump(),
                "tools": [s.model_dump() for s in tools.schemas],
                "tool_gateway": {"url": f"{base}/tools", "token": token},
                "callback_url": f"{base}/result",
                "limits": {
                    "max_tool_calls": limits.max_tool_calls,
                    "deadline": deadline.isoformat(),
                },
            }
        ).encode()
        response = await self._send(body, limits.timeout_seconds)
        await events.record(
            "external_agent_replied",
            {
                "status_code": response.status_code,
                "latency_ms": round((time.monotonic() - started) * 1000),
            },
        )
        if response.status_code == 202:
            return await self._wait_for_result(started, limits)
        if response.status_code >= 500:
            raise ExternalAgentUnavailableError(f"the agent answered {response.status_code}")
        if response.status_code != 200:
            return AgentResult("", "agent_error", {"error": f"status {response.status_code}"})
        return self._result(response.content)

    async def _send(self, body: bytes, timeout: float) -> httpx.Response:
        headers = signed_headers(self._link.signing_secret, body, self._link.attempt_id)
        async with self._client() as client:
            for attempt in range(CONNECT_RETRIES + 1):
                try:
                    return await post(
                        client,
                        self._settings.url,
                        body,
                        headers,
                        policy=self._link.policy,
                        timeout=timeout,
                    )
                except httpx.ConnectError as exc:
                    if attempt == CONNECT_RETRIES:
                        raise ExternalAgentUnavailableError(
                            "the agent could not be reached"
                        ) from exc
                    await asyncio.sleep(0.5 * 2**attempt)
                except EndpointBlockedError as exc:
                    raise ExternalAgentUnavailableError(f"the agent URL is blocked: {exc}") from exc
                except ResponseTooLargeError:
                    return httpx.Response(413)
                except httpx.TimeoutException as exc:
                    raise ExternalAgentUnavailableError("the agent did not answer in time") from exc
        raise ExternalAgentUnavailableError("the agent could not be reached")

    async def _wait_for_result(self, started: float, limits: RunLimits) -> AgentResult:
        while time.monotonic() - started < limits.timeout_seconds:
            posted = await self._link.posted_result()
            if posted is not None:
                return self._result(json.dumps(posted).encode())
            await asyncio.sleep(self._poll_seconds)
        return AgentResult("", "timeout")

    def _result(self, content: bytes) -> AgentResult:
        try:
            reply = AgentReply.model_validate_json(content)
        except ValidationError:
            return AgentResult("", "agent_error", {"error": "the reply was not valid JSON"})
        # Usage comes from the agent itself and cannot be checked, so it is labeled as such.
        return AgentResult(
            reply.final_response, "model_final", {"usage_self_reported": reply.usage}
        )


class HandshakeResult(BaseModel):
    ok: bool
    status_code: int | None = None
    latency_ms: int | None = None
    problem: str | None = None


async def handshake(
    url: str,
    secret: str,
    policy: GuardPolicy,
    timeout: float = 10.0,
    client: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(trust_env=False),
) -> HandshakeResult:
    """Ping an agent with a signed handshake before trusting it with a real run."""
    body = json.dumps({"protocol_version": PROTOCOL_VERSION, "kind": "handshake"}).encode()
    started = time.monotonic()
    try:
        async with client() as http:
            response = await post(
                http,
                url,
                body,
                signed_headers(secret, body, "handshake"),
                policy=policy,
                timeout=timeout,
            )
    except EndpointBlockedError as exc:
        return HandshakeResult(ok=False, problem=f"blocked: {exc}")
    except (httpx.HTTPError, ResponseTooLargeError) as exc:
        return HandshakeResult(ok=False, problem=f"unreachable: {type(exc).__name__}")
    latency = round((time.monotonic() - started) * 1000)
    if response.status_code != 200:
        return HandshakeResult(
            ok=False,
            status_code=response.status_code,
            latency_ms=latency,
            problem="the agent must answer a handshake with 200",
        )
    try:
        version = response.json().get("protocol_version")
    except (ValueError, AttributeError):
        version = None
    if version != PROTOCOL_VERSION:
        return HandshakeResult(
            ok=False,
            status_code=200,
            latency_ms=latency,
            problem=f'the reply must include "protocol_version": "{PROTOCOL_VERSION}"',
        )
    return HandshakeResult(ok=True, status_code=200, latency_ms=latency)
