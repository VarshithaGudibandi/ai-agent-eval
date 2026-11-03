from app.evaluators.response.text import clauses, states
from app.evaluators.spec_parts import Claim

RESET = Claim(name="reset", code="FALSE_RESET_CLAIM", any_of=[r"reset your password"])


def test_clauses_split_sentences_and_contrasts() -> None:
    assert clauses("I checked; it is fine. Sadly, the reset failed, but access works!") == [
        "I checked",
        "it is fine.",
        "Sadly, the reset failed",
        "access works!",
    ]


def test_a_claim_is_stated_only_when_not_negated_or_conditional() -> None:
    assert states(RESET, "I've reset your password.")
    assert not states(RESET, "I couldn't reset your password.")
    assert not states(RESET, "Once your manager approves, I will reset your password.")
