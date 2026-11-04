import httpx
import pytest

from app.agents.http_guard import (
    EndpointBlockedError,
    GuardPolicy,
    ResponseTooLargeError,
    post,
    resolve,
)

STRICT = GuardPolicy(allow_local=False, require_https=False, max_response_bytes=1_000)
LOCAL = GuardPolicy(allow_local=True, require_https=False, max_response_bytes=1_000)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/agent",
        "http://10.0.0.5/agent",
        "http://192.168.1.10/agent",
        "http://[::1]/agent",
        "http://[::ffff:127.0.0.1]/agent",
        "http://localhost/agent",
    ],
)
async def test_local_and_private_addresses_need_the_local_setting(url: str) -> None:
    with pytest.raises(EndpointBlockedError):
        await resolve(url, STRICT)
    await resolve(url, LOCAL)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://[fd00:ec2::254]/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
    ],
)
async def test_metadata_and_other_special_addresses_are_never_allowed(url: str) -> None:
    with pytest.raises(EndpointBlockedError):
        await resolve(url, LOCAL)


async def test_scheme_and_https_rules() -> None:
    with pytest.raises(EndpointBlockedError):
        await resolve("ftp://8.8.8.8/x", STRICT)
    https_only = GuardPolicy(allow_local=False, require_https=True, max_response_bytes=1)
    with pytest.raises(EndpointBlockedError, match="https"):
        await resolve("http://8.8.8.8/x", https_only)


async def test_requests_go_to_the_checked_address_with_the_original_host() -> None:
    pinned, host = await resolve("http://localhost:9000/agent?x=1", LOCAL)
    assert pinned.startswith("http://127.0.0.1:9000/agent") or pinned.startswith(
        "http://[::1]:9000/agent"
    )
    assert host == "localhost:9000"


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_redirects_are_not_followed() -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    async with _client(httpx.MockTransport(redirect)) as client:
        response = await post(client, "http://127.0.0.1/a", b"{}", {}, policy=LOCAL, timeout=2)
    assert response.status_code == 302


async def test_oversized_replies_are_cut_off() -> None:
    def huge(request: httpx.Request) -> httpx.Response:
        assert request.headers["host"] == "127.0.0.1"
        return httpx.Response(200, content=b"x" * 5_000)

    async with _client(httpx.MockTransport(huge)) as client:
        with pytest.raises(ResponseTooLargeError):
            await post(client, "http://127.0.0.1/a", b"{}", {}, policy=LOCAL, timeout=2)
