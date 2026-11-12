import pytest
from pydantic import ValidationError

from app.scoring.definitions import EvalSpecDefinition, ScorerDefinition


def test_defaults_are_written_out_so_equal_scorers_hash_equal() -> None:
    short = ScorerDefinition(key="turns", check="turn_limit")
    spelled = ScorerDefinition(
        key="turns", check="turn_limit", params={"max_turns": 10}, severity="hard", threshold=1
    )
    assert short.params == {"max_turns": 10}
    assert short.content_hash() == spelled.content_hash()
    assert (
        short.content_hash()
        != ScorerDefinition(key="turns", check="turn_limit", params={"max_turns": 3}).content_hash()
    )


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"key": "x", "check": "vibes"}, "unknown check 'vibes'"),
        ({"key": "x", "check": "tool_order", "params": {"tool": "a"}}, "tool_order params: after"),
        ({"key": "x", "check": "turn_limit", "params": {"max": 3}}, "Extra inputs"),
        ({"key": "Bad Key", "check": "run_completed"}, "pattern"),
        ({"key": "x", "check": "run_completed", "threshold": 2}, "less than or equal"),
    ],
)
def test_a_bad_scorer_says_what_is_wrong(fields: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ScorerDefinition.model_validate(fields)


def test_scorer_keys_are_unique_in_a_spec() -> None:
    with pytest.raises(ValidationError, match="unique: ran"):
        EvalSpecDefinition.model_validate(
            {"scorers": [{"key": "ran", "check": "run_completed"}] * 2}
        )
