import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.settings import Settings

# Cloud metadata services. Most sit in link-local space, which is always blocked, but the
# AWS IPv6 one does not.
_METADATA = {ipaddress.ip_address("fd00:ec2::254")}


class EndpointBlockedError(Exception):
    """The agent URL points somewhere the platform must never call."""


class ResponseTooLargeError(Exception):
    pass


@dataclass(frozen=True)
class GuardPolicy:
    allow_local: bool
    require_https: bool
    max_response_bytes: int

    @classmethod
    def of(cls, settings: Settings) -> "GuardPolicy":
        return cls(
            allow_local=settings.allow_local_agents,
            require_https=settings.require_https_agents,
            max_response_bytes=settings.agent_response_max_bytes,
        )


def _check_address(address: str, policy: GuardPolicy) -> None:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in _METADATA or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        raise EndpointBlockedError(f"{address} is never allowed")
    # Loopback and private come first: Python also counts ::1 as reserved.
    if ip.is_loopback or ip.is_private:
        if not policy.allow_local:
            raise EndpointBlockedError(f"{address} is a local or private address")
    elif ip.is_reserved:
        raise EndpointBlockedError(f"{address} is a reserved address")


async def resolve(url: str, policy: GuardPolicy) -> tuple[str, str]:
    """Check a URL and pick the address to connect to.

    The check runs on the resolved addresses, every time, so a name that later resolves
    somewhere else (DNS rebinding) is caught. Returns (url aimed at that address, host).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise EndpointBlockedError("the agent URL must be http or https with a host")
    if policy.require_https and parts.scheme != "https":
        raise EndpointBlockedError("the agent URL must use https")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        found = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise EndpointBlockedError(f"{parts.hostname} does not resolve") from exc
    addresses = sorted({str(info[4][0]) for info in found})
    # Every address must pass, or a name could mix a public address with a private one.
    for address in addresses:
        _check_address(address, policy)
    chosen = addresses[0]
    literal = f"[{chosen}]" if ":" in chosen else chosen
    pinned = parts._replace(netloc=f"{literal}:{port}")
    return urlunsplit(pinned), parts.netloc


async def post(
    client: httpx.AsyncClient,
    url: str,
    content: bytes,
    headers: dict[str, str],
    *,
    policy: GuardPolicy,
    timeout: float,
) -> httpx.Response:
    """POST to an external agent through the guard, never following redirects."""
    pinned, host = await resolve(url, policy)
    hostname = urlsplit(url).hostname or ""
    request = client.build_request(
        "POST",
        pinned,
        content=content,
        headers={**headers, "host": host, "content-type": "application/json"},
        timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
        extensions={"sni_hostname": hostname},
    )
    response = await client.send(request, stream=True, follow_redirects=False)
    try:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > policy.max_response_bytes:
                raise ResponseTooLargeError(f"over {policy.max_response_bytes} bytes")
    finally:
        await response.aclose()
    return httpx.Response(response.status_code, headers=response.headers, content=bytes(body))
