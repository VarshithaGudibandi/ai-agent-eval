import json
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from agent_eval.client import PlatformClient
from agent_eval.sender import BackgroundSender


def _sender(handler: Callable[[httpx.Request], httpx.Response], **options: Any) -> BackgroundSender:
    transport = httpx.MockTransport(handler)
    return BackgroundSender(
        lambda: PlatformClient("http://platform", "aet_fake", transport=transport),
        linger_seconds=0.05,
        **options,
    )


def _trace(n: int) -> dict[str, Any]:
    return {"trace_id": f"t{n}", "spans": []}


def test_queued_traces_go_out_in_batches_and_feedback_on_its_own() -> None:
    seen: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        assert request.headers["authorization"] == "Bearer aet_fake"
        if request.url.path == "/api/v1/traces":
            ids = [t["trace_id"] for t in body["traces"]]
            return httpx.Response(200, json={"accepted": ids, "duplicates": [], "rejected": []})
        return httpx.Response(201, json={"feedback_id": body["feedback_id"], "created": True})

    sender = _sender(handler)
    for n in range(150):
        sender.submit_trace(_trace(n))
    sender.submit_feedback({"feedback_id": "f1", "trace_id": "t1", "score": 1.0})
    sender.close(timeout=5)
    sent = [
        t["trace_id"] for path, body in seen if path == "/api/v1/traces" for t in body["traces"]
    ]
    assert sent == [f"t{n}" for n in range(150)]
    assert max(len(b["traces"]) for p, b in seen if p == "/api/v1/traces") <= 100
    assert [b for p, b in seen if p == "/api/v1/feedback"] == [
        {"feedback_id": "f1", "trace_id": "t1", "score": 1.0}
    ]


def test_a_full_queue_drops_instead_of_waiting() -> None:
    release = threading.Event()

    def stuck(request: httpx.Request) -> httpx.Response:
        release.wait(5)
        return httpx.Response(200, json={"accepted": [], "duplicates": [], "rejected": []})

    sender = _sender(stuck, max_queue=5)
    started = time.monotonic()
    for n in range(500):
        sender.submit_trace(_trace(n))
    assert time.monotonic() - started < 0.5
    assert sender.dropped >= 490
    release.set()
    sender.close(timeout=5)


def test_a_failing_platform_is_retried_a_few_times_then_given_up() -> None:
    calls: list[int] = []

    def failing(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"error": "unavailable"})

    sender = _sender(failing, attempts=2)
    sender.submit_trace(_trace(1))
    sender.close(timeout=5)
    assert len(calls) == 2 and sender.dropped == 1


def test_a_refused_request_is_not_retried() -> None:
    calls: list[int] = []

    def refusing(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"error": "unauthorized"})

    sender = _sender(refusing)
    sender.submit_trace(_trace(1))
    sender.close(timeout=5)
    assert len(calls) == 1 and sender.dropped == 1
