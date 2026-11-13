from dataclasses import replace

from app.scoring.checks import (
    MatchesExpectedParams,
    NoParams,
    matches_expected,
    valid_tool_arguments,
)
from tests.scoring.golden import golden, view


def test_gemini_shaped_declarations_with_capital_types_are_understood() -> None:
    run = view(golden("refund"), "refund")
    gemini = {
        "tools": [
            {
                "function_declarations": [
                    {
                        "name": "get_order",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {"order_id": {"type": "STRING"}},
                            "required": ["order_id"],
                        },
                    },
                    {"name": "get_return"},
                    {"name": "issue_refund"},
                ]
            }
        ]
    }
    outcomes = valid_tool_arguments(replace(run, config=gemini), NoParams())
    assert [o.value for o in outcomes] == [1.0, 1.0, 1.0]
    assert valid_tool_arguments(replace(run, config={}), NoParams()) == []


def test_matches_expected_needs_the_field_in_the_expected_outcome() -> None:
    run = view(golden("refund"), "refund")
    params = MatchesExpectedParams(field="refund", tool="issue_refund", argument="amount")
    assert [o.value for o in matches_expected(run, params)] == [1.0]
    assert matches_expected(replace(run, expected={"other": 1}), params) == []
    assert matches_expected(replace(run, expected=None), params) == []
    # 59 and 59.0 are the same amount.
    assert [o.value for o in matches_expected(replace(run, expected={"refund": 59}), params)] == [
        1.0
    ]
