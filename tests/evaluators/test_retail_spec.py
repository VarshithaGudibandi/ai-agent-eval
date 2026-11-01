import pytest
from pydantic import ValidationError

from app.evaluators.retail_spec import DEFAULT_ORDER_RULES, RetailFacts, RetailSpec, is_retail
from tests.evaluators.traces import Trace, retail_task


def test_defaults_require_investigation_before_refunds() -> None:
    spec = RetailSpec.model_validate({"expected_refunds": []})
    assert tuple(spec.order_rules) == DEFAULT_ORDER_RULES
    assert spec.max_identical_calls == 3


@pytest.mark.parametrize(
    "bad",
    [
        {"expected_refunds": [], "surprise": 1},
        {"expected_refunds": [{"order_id": "o", "amount_minor": 0}]},
        {"expected_refunds": [], "response": {"must_include": [{"name": "x", "any_of": ["("]}]}},
        {
            "expected_refunds": [],
            "response": {"must_not_claim": [{"name": "x", "code": "lower", "any_of": ["a"]}]},
        },
    ],
)
def test_bad_specs_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RetailSpec.model_validate(bad)


def test_facts_come_from_the_fixture() -> None:
    facts = RetailFacts(Trace().context({"expected_refunds": []}))
    assert facts.customer_id == "cust_12"
    assert facts.is_foreign_order("ord_2002") and not facts.is_foreign_order("ord_1001")
    # An id nobody owns is not foreign: it does not exist.
    assert not facts.is_foreign_order("ord_nope")
    assert facts.is_foreign_ticket("tkt_9")
    assert facts.accepted_returns == {"ord_1001"}
    assert facts.prior_refunded == {"ord_1002": 3000}


def test_retail_tasks_are_recognized_by_environment_name() -> None:
    assert is_retail(retail_task({"expected_refunds": []}))
