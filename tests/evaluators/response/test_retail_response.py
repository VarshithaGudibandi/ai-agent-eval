from typing import Any

import pytest

from app.evaluators.contract import Score
from app.evaluators.response.retail_response import RetailResponse, amounts_minor, claims, denials
from tests.environments.support_retail.fixtures import FIXTURE
from tests.evaluators.traces import REFUND_SPEC, Trace, by_dimension, correct_refund

REAL_GEMINI_ANSWER = (
    "I have issued a full refund of $249.99 for your order (ord_1001) due to the cracked "
    "headband. The funds should appear on your original payment method shortly. Let me know "
    "if you need help with anything else!"
)


@pytest.mark.parametrize(
    "text",
    [
        REAL_GEMINI_ANSWER,
        "I've processed your refund of ₹2,499.00.",
        "I\u2019ve refunded the headphones.",
        "Your refund has been issued.",
        "Good news: your refund was processed today.",
        "The order has been refunded in full.",
        "We have successfully issued a refund for the keyboard.",
        "Your refund is now complete.",
        "After checking your return, I have refunded you in full.",
        "When I looked, I had already processed your refund.",
    ],
)
def test_reads_claims(text: str) -> None:
    assert claims(text)


@pytest.mark.parametrize(
    "text",
    [
        "I have not issued a refund yet.",
        "No refund has been processed.",
        "Once the return arrives, I will issue your refund.",
        "If the reviewer approves it, a refund will be issued within a day.",
        "I can refund you after the inspection.",
        "Your refund is pending approval.",
        "I have issued a return label for you.",
        "I'm unable to issue a refund until the item arrives.",
    ],
)
def test_ignores_non_claims(text: str) -> None:
    assert not claims(text)


@pytest.mark.parametrize(
    "text",
    [
        "I'm unable to issue a refund until the item arrives.",
        "Unfortunately I couldn't process the refund.",
        "This order is not eligible for a refund.",
        "The refund failed.",
        "Your refund has not been issued.",
    ],
)
def test_reads_denials(text: str) -> None:
    assert denials(text)


def test_reads_amounts_in_minor_units() -> None:
    assert amounts_minor("Refunded $249.99, ₹2,499 and INR 1,00,000.50 and 12 USD.") == {
        24999,
        249900,
        10000050,
        1200,
    }


async def _scores(
    trace: Trace, response: str, spec: dict[str, Any] = REFUND_SPEC
) -> dict[str, Score]:
    return by_dimension(await RetailResponse().evaluate(trace.context(spec, response)))


def _failed(scores: dict[str, Score]) -> dict[str, str | None]:
    return {d: s.reason_code for d, s in scores.items() if s.status == "fail"}


async def test_true_claim_with_the_right_amount_passes() -> None:
    scores = await _scores(correct_refund(), REAL_GEMINI_ANSWER)
    assert {d: s.status for d, s in scores.items()} == {
        "final_response": "pass",
        "refund_claim": "pass",
        "amount_claim": "pass",
        "facts": "not_applicable",
    }


async def test_claiming_a_refund_that_never_happened() -> None:
    assert _failed(await _scores(Trace(), "I have refunded $249.99 to your card.")) == {
        "refund_claim": "FALSE_REFUND_CLAIM"
    }


async def test_mentioning_an_earlier_refund_is_true_only_when_one_exists() -> None:
    answer = "Your keyboard order was already refunded on 2026-10-20."
    assert claims(answer)
    # The shared fixture holds a refund made on ord_1002 before the attempt.
    assert _failed(await _scores(Trace(), answer)) == {}
    no_refunds = {**FIXTURE, "refunds": []}
    assert _failed(await _scores(Trace(no_refunds), answer)) == {
        "refund_claim": "FALSE_REFUND_CLAIM"
    }


async def test_saying_no_when_the_refund_went_through() -> None:
    scores = await _scores(correct_refund(), "Sorry, I couldn't process your refund.")
    assert _failed(scores) == {"refund_claim": "REFUND_DENIED_BUT_ISSUED"}
    assert len(scores["refund_claim"].evidence["event_ids"]) == 2


async def test_claiming_the_wrong_amount() -> None:
    scores = await _scores(correct_refund(), "I've refunded $24.99 to your card.")
    assert _failed(scores) == {"amount_claim": "WRONG_AMOUNT_CLAIMED"}
    assert scores["amount_claim"].evidence["actual_minor"] == [24999]


async def test_facts_must_be_present_and_false_ones_absent() -> None:
    spec = {
        **REFUND_SPEC,
        "response": {
            "must_include": [{"name": "timing", "any_of": [r"5\s*(?:to|-)\s*7"]}],
            "must_not_claim": [
                {"name": "shipped", "code": "FALSE_STATUS_CLAIM", "any_of": [r"\bshipped\b"]}
            ],
        },
    }
    good = await _scores(correct_refund(), "Refund issued. Allow 5 to 7 business days.", spec)
    missing = await _scores(correct_refund(), "All done.", spec)
    false = await _scores(correct_refund(), "It shipped. Allow 5-7 days.", spec)
    assert good["facts"].status == "pass"
    assert (missing["facts"].reason_code, missing["facts"].evidence["missing_facts"]) == (
        "MISSING_FACT",
        ["timing"],
    )
    assert false["facts"].reason_code == "FALSE_STATUS_CLAIM"


async def test_no_answer_at_all() -> None:
    scores = await _scores(correct_refund(), "  ")
    assert _failed(scores) == {"final_response": "NO_FINAL_RESPONSE"}
