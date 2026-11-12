import json
import logging
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent_eval import current_trace_id, init, log_feedback, shutdown, traced, wrap_gemini
from tests.sdk.conftest import CONFIG
from tests.sdk.fake_gemini import FakeGemini, reply

client = wrap_gemini(FakeGemini(lambda *_: reply("It shipped.")))


@traced
def lookup(order: object) -> str:
    return "shipped"


@traced
def handle_request(message: str) -> tuple[str, str | None]:
    lookup(object())
    answer = client.models.generate_content(model="gemini-3.5-flash-lite", contents=message)
    return answer.candidates[0].content.parts[0].text, current_trace_id()


class Platform(ThreadingHTTPServer):
    """A local stand-in for the platform that answers every request with one status."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.received: list[tuple[str, Any]] = []
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    server: Platform

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.server.received.append((self.path, body))
        if "traces" in body:
            ids = [t["trace_id"] for t in body["traces"]]
            payload = {"accepted": ids, "duplicates": [], "rejected": []}
        else:
            payload = {"feedback_id": body.get("feedback_id"), "created": True}
        data = json.dumps(payload).encode()
        self.send_response(self.server.status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def _serve(status: int) -> Iterator[Platform]:
    platform = Platform(status)
    thread = threading.Thread(target=platform.serve_forever, daemon=True)
    thread.start()
    yield platform
    platform.shutdown()
    platform.server_close()


@pytest.fixture
def healthy() -> Iterator[Platform]:
    yield from _serve(200)


@pytest.fixture
def broken() -> Iterator[Platform]:
    yield from _serve(500)


@pytest.fixture
def silent() -> Iterator[str]:
    # Accepts connections into its backlog and never answers.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    sock.close()


@pytest.fixture
def token() -> Iterator[str]:
    yield "aet_" + secrets.token_urlsafe(24)
    shutdown(timeout=0.2)


def _run_many(count: int = 200) -> float:
    started = time.monotonic()
    for n in range(count):
        text, trace_id = handle_request(f"order {n}")
        assert text == "It shipped." and trace_id is not None
    return time.monotonic() - started


def test_live_traces_and_feedback_reach_the_platform(healthy: Platform, token: str) -> None:
    init("support-bot", CONFIG, environment="staging", label="v3", url=healthy.url, token=token)
    _, trace_id = handle_request("where is my order?")
    log_feedback(trace_id or "", 0, "Wrong order")
    shutdown(timeout=5)
    traces = [
        t for path, body in healthy.received if path == "/api/v1/traces" for t in body["traces"]
    ]
    [trace] = traces
    assert trace["trace_id"] == trace_id and trace["tags"] == {"environment": "staging"}
    assert {s["kind"] for s in trace["spans"]} == {"step", "tool", "model"}
    [feedback] = [body for path, body in healthy.received if path == "/api/v1/feedback"]
    assert (feedback["trace_id"], feedback["score"], feedback["comment"]) == (
        trace_id,
        0.0,
        "Wrong order",
    )


@pytest.mark.parametrize("where", ["unreachable", "broken", "silent"])
def test_live_mode_never_slows_or_breaks_the_app(
    where: str,
    request: pytest.FixtureRequest,
    token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    url = {
        "unreachable": "http://127.0.0.1:1",
        "broken": lambda: request.getfixturevalue("broken").url,
        "silent": lambda: request.getfixturevalue("silent"),
    }[where]
    with caplog.at_level(logging.DEBUG, logger="agent_eval"):
        init("support-bot", CONFIG, url=url if isinstance(url, str) else url(), token=token)
        # The calls only queue their traces, so the platform's state cannot slow them.
        assert _run_many() < 2.0
        log_feedback("t-1", 1)
        shutdown(timeout=0.2)
    assert token not in caplog.text


def test_misuse_of_the_sdk_never_raises(
    healthy: Platform, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    def broken_hook(value: object) -> object:
        raise RuntimeError("hook bug")

    with caplog.at_level(logging.WARNING, logger="agent_eval"):
        log_feedback("t-1", 1)
        init("support-bot", CONFIG, url=healthy.url, token=None)
        assert handle_request("no token set")[0] == "It shipped."
        init("support-bot", CONFIG, url=healthy.url, token=token, redact=broken_hook)
        assert handle_request("hook breaks")[0] == "It shipped."
        log_feedback("t-1", 7)
        log_feedback("t-1", "great")  # type: ignore[arg-type]
        log_feedback(None, 1, comment=object())  # type: ignore[arg-type]
        init(None, None)  # type: ignore[arg-type]
        shutdown(timeout=5)
    assert "needs init()" in caplog.text and "must be from 0 to 1" in caplog.text


def test_a_sender_thread_that_cannot_start_does_not_break_the_app(
    healthy: Platform, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    init("support-bot", CONFIG, url=healthy.url, token=token)

    def refuse(self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    # A process at its thread limit, as under a tight container ulimit.
    monkeypatch.setattr(threading.Thread, "start", refuse)
    assert handle_request("hello")[0] == "It shipped."
