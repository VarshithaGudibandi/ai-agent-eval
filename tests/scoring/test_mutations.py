"""The scorer mutation suite. Each planted fault must be caught by its named scorer and by
no other, and the untouched golden runs must pass every scorer."""

from collections.abc import Callable
from typing import Any

import pytest

from app.scoring.definitions import ScorerDefinition
from app.scoring.engine import Scorer, score
from tests.scoring.golden import GOLDEN, golden, view

SPEC = [
    Scorer(f"scr_{d.key}", d)
    for d in [
        ScorerDefinition(key="run_completed", check="run_completed"),
        ScorerDefinition(key="tool_errors", check="tool_errors"),
        ScorerDefinition(key="valid_tool_arguments", check="valid_tool_arguments"),
        ScorerDefinition(key="turn_limit", check="turn_limit", params={"max_turns": 4}),
        ScorerDefinition(
            key="refund_after_lookup",
            check="tool_order",
            params={"tool": "issue_refund", "after": ["get_order"]},
        ),
        ScorerDefinition(
            key="never_delete_accounts", check="tool_not_called", params={"tool": "delete_account"}
        ),
        ScorerDefinition(
            key="lookup_limit",
            check="tool_call_limit",
            params={"tool": "get_order", "max_calls": 2},
        ),
        ScorerDefinition(
            key="confirms_refund",
            check="reply_contains",
            params={"text": "refunded"},
            applies_when={"tools_used": ["issue_refund"]},
        ),
        ScorerDefinition(
            key="no_ai_disclaimer", check="reply_excludes", params={"text": "as an AI"}
        ),
        ScorerDefinition(
            key="refund_amount",
            check="matches_expected",
            params={"field": "refund", "tool": "issue_refund", "argument": "amount"},
        ),
    ]
]

Trace = dict[str, Any]


def _span(trace: Trace, name: str) -> dict[str, Any]:
    return next(s for s in trace["spans"] if s["name"] == name)


def _root(trace: Trace) -> dict[str, Any]:
    return next(s for s in trace["spans"] if "parent_id" not in s)


def _add(trace: Trace, kind: str, name: str, **fields: Any) -> None:
    root = _root(trace)
    span = {
        "id": f"extra{len(trace['spans'])}",
        "parent_id": root["id"],
        "kind": kind,
        "name": name,
        # Last in the run: after every other step has finished.
        "start": root["end"],
        "end": root["end"],
        **fields,
    }
    trace["spans"].append(span)


def root_error(t: Trace) -> None:
    _root(t)["error"] = "RuntimeError: the agent crashed"


def tool_error(t: Trace) -> None:
    _span(t, "get_order")["error"] = "TimeoutError: orders service did not answer"


def bad_argument(t: Trace) -> None:
    _span(t, "get_order")["input"]["order_id"] = 1001


def undeclared_tool(t: Trace) -> None:
    _add(t, "tool", "cancel_order", input={"order_id": "ord_1001"})


def too_many_turns(t: Trace) -> None:
    for _ in range(2):
        _add(t, "model", "generate_content", provider="gemini", model="gemini-flash-lite-latest")


def refund_before_lookup(t: Trace) -> None:
    refund, root = _span(t, "issue_refund"), _root(t)
    refund["start"] = refund["end"] = root["start"]


def deleted_account(t: Trace) -> None:
    _add(t, "tool", "delete_account", input={"customer_id": "c-17"})


def repeated_lookups(t: Trace) -> None:
    for _ in range(2):
        _add(t, "tool", "get_order", input={"order_id": "ord_1001"})


def no_confirmation(t: Trace) -> None:
    _root(t)["output"] = {"reply": "Done."}


def ai_disclaimer(t: Trace) -> None:
    _root(t)["output"] = {"reply": "As an AI, I can say ord_1002 has shipped."}


def wrong_amount(t: Trace) -> None:
    _span(t, "issue_refund")["input"]["amount"] = 20.0


def refund_not_allowed(t: Trace) -> None:
    _add(t, "tool", "issue_refund", input={"order_id": "ord_1002", "amount": 34.5})


MUTATIONS: list[tuple[str, Callable[[Trace], None], str]] = [
    ("refund", root_error, "run_completed"),
    ("refund", tool_error, "tool_errors"),
    ("refund", bad_argument, "valid_tool_arguments"),
    ("status", undeclared_tool, "valid_tool_arguments"),
    ("refund", too_many_turns, "turn_limit"),
    ("refund", refund_before_lookup, "refund_after_lookup"),
    ("status", deleted_account, "never_delete_accounts"),
    ("refund", repeated_lookups, "lookup_limit"),
    ("refund", no_confirmation, "confirms_refund"),
    ("status", ai_disclaimer, "no_ai_disclaimer"),
    ("refund", wrong_amount, "refund_amount"),
    ("not-delivered", refund_not_allowed, "refund_amount"),
]


def _failing(trace: Trace, case: str) -> set[str]:
    results = score(view(trace, case), SPEC)
    assert [r.error for r in results if r.error] == []
    return {r.key for r in results if not r.passed}


@pytest.mark.parametrize("case", sorted(GOLDEN))
def test_the_golden_runs_pass_every_scorer(case: str) -> None:
    assert _failing(golden(case), case) == set()


def test_every_scorer_in_the_spec_has_a_planted_fault() -> None:
    assert {key for _, _, key in MUTATIONS} == {s.definition.key for s in SPEC}


@pytest.mark.parametrize(
    ("case", "mutate", "scorer"), MUTATIONS, ids=[m.__name__ for _, m, _ in MUTATIONS]
)
def test_each_fault_is_caught_by_its_scorer_and_no_other(
    case: str, mutate: Callable[[Trace], None], scorer: str
) -> None:
    trace = golden(case)
    mutate(trace)
    assert _failing(trace, case) == {scorer}
