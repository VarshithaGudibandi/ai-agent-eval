from typing import Any

import pytest
from pydantic import ValidationError

from app.ingest.trace_format import Trace
from tests.ingest.sample_traces import support_trace


def _spans(trace: dict[str, Any]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = trace["spans"]
    return spans


def _rejected(trace: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Trace.model_validate(trace)


def test_the_sample_trace_is_valid_and_has_one_root() -> None:
    trace = Trace.model_validate(support_trace())
    assert trace.root.id == "root"
    assert [s.kind for s in trace.spans] == ["step", "model", "tool", "model"]


def test_span_ids_must_be_unique() -> None:
    trace = support_trace()
    _spans(trace)[2]["id"] = "m1"
    _rejected(trace, "unique")


def test_exactly_one_root() -> None:
    no_root = support_trace()
    _spans(no_root)[0]["parent_id"] = "m1"
    _rejected(no_root, "exactly one root")
    two = support_trace()
    _spans(two)[1]["parent_id"] = None
    _rejected(two, "exactly one root")


def test_parents_must_exist() -> None:
    trace = support_trace()
    _spans(trace)[2]["parent_id"] = "nowhere"
    _rejected(trace, "not in the trace")


def test_a_loop_beside_the_root_is_refused() -> None:
    trace = support_trace()
    _spans(trace)[1]["parent_id"] = "t1"
    _spans(trace)[2]["parent_id"] = "m1"
    _rejected(trace, "loop")


def test_a_deep_chain_is_checked_quickly() -> None:
    trace = support_trace()
    chain = [dict(_spans(trace)[0])]
    for n in range(4999):
        chain.append({**_spans(trace)[2], "id": f"s{n}", "parent_id": chain[-1]["id"]})
    trace["spans"] = chain
    assert len(Trace.model_validate(trace).spans) == 5000


def test_spans_cannot_end_before_they_start_and_need_time_zones() -> None:
    backwards = support_trace()
    _spans(backwards)[2]["end"] = "2026-11-12T10:00:00.000Z"
    _rejected(backwards, "ends before it starts")
    naive = support_trace()
    _spans(naive)[2]["start"] = "2026-11-12T10:00:01"
    _rejected(naive, "timezone")


def test_model_spans_name_their_model() -> None:
    trace = support_trace()
    del _spans(trace)[1]["model"]
    _rejected(trace, "must name its model")


def test_unknown_fields_and_odd_trace_ids_are_refused() -> None:
    _rejected(support_trace(workspace_id="ws_other"), "Extra inputs")
    _rejected(support_trace("has spaces"), "pattern")
    _rejected(support_trace(spans=[]), "at least 1")
