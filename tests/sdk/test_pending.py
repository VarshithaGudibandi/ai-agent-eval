import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from agent_eval.client import PlatformClient
from agent_eval.pending import PendingTraces, upload

EXPERIMENT = "exp_1"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> PlatformClient:
    return PlatformClient("http://platform", "aet_fake", transport=httpx.MockTransport(handler))


def _store(tmp_path: Path, count: int) -> PendingTraces:
    store = PendingTraces(tmp_path)
    for n in range(count):
        store.add(EXPERIMENT, {"trace_id": f"t{n}", "spans": []})
    return store


def _platform(
    seen: list[str], rejected: frozenset[str] = frozenset(), max_traces: int = 100
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": EXPERIMENT})
        if request.url.path.endswith("/finish"):
            return httpx.Response(200, json={"id": EXPERIMENT, "status": "finished"})
        ids = [t["trace_id"] for t in json.loads(request.content)["traces"]]
        if len(ids) > max_traces:
            return httpx.Response(413, json={"error": "batch_too_large"})
        seen.extend(ids)
        body: dict[str, Any] = {
            "accepted": [i for i in ids if i not in rejected],
            "duplicates": [],
            "rejected": [{"trace_id": i, "error": "bad"} for i in ids if i in rejected],
        }
        return httpx.Response(200, json=body)

    return handler


def test_a_line_cut_short_by_a_crash_is_skipped(tmp_path: Path) -> None:
    store = _store(tmp_path, 2)
    with (tmp_path / f"{EXPERIMENT}.jsonl").open("ab") as file:
        file.write(b'{"trace_id": "t2", "spa')
    seen: list[str] = []
    result = upload(_client(_platform(seen)), store, EXPERIMENT)
    assert (result.uploaded, seen) == (2, ["t0", "t1"])
    assert store.experiments() == []


def test_traces_are_kept_when_the_experiment_cannot_be_found(tmp_path: Path) -> None:
    store = _store(tmp_path, 3)

    def wrong_workspace(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not_found"})

    result = upload(_client(wrong_workspace), store, EXPERIMENT)
    assert result.remaining == 3
    assert len(store.traces(EXPERIMENT)) == 3


def test_rejected_traces_are_dropped_and_counted(tmp_path: Path) -> None:
    store = _store(tmp_path, 3)
    seen: list[str] = []
    result = upload(_client(_platform(seen, rejected=frozenset({"t1"}))), store, EXPERIMENT)
    assert (result.uploaded, result.rejected, result.remaining) == (2, 1, 0)
    assert store.experiments() == []


def test_a_platform_with_a_lower_size_limit_gets_smaller_batches(tmp_path: Path) -> None:
    store = _store(tmp_path, 10)
    seen: list[str] = []
    result = upload(_client(_platform(seen, max_traces=3)), store, EXPERIMENT)
    assert (result.uploaded, result.remaining) == (10, 0)
    assert sorted(seen) == sorted(f"t{n}" for n in range(10))


def test_an_upload_that_stops_halfway_keeps_only_what_is_left(tmp_path: Path) -> None:
    store = _store(tmp_path, 150)
    seen: list[str] = []
    healthy = _platform(seen)

    def dies_after_one_batch(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and seen:
            return httpx.Response(503, json={"error": "unavailable"})
        return healthy(request)

    result = upload(_client(dies_after_one_batch), store, EXPERIMENT)
    assert (result.uploaded, result.remaining) == (100, 50)
    assert [t["trace_id"] for t in store.traces(EXPERIMENT)] == [f"t{n}" for n in range(100, 150)]
