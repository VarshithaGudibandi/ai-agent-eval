from app.environments.support_retail.policies import search


def test_search_ranks_by_matching_words() -> None:
    best = search("how long until my refund arrives, business days")[0]
    assert best.policy_id == "POL-7"


def test_search_matches_plurals_and_ignores_case() -> None:
    assert "POL-6" in [p.policy_id for p in search("Closing TICKETS")]


def test_search_returns_nothing_for_unrelated_words() -> None:
    assert search("weather forecast") == []


def test_search_is_capped() -> None:
    assert len(search("refund order return approval ticket")) == 3
