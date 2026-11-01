from app.evaluators.registry import default_evaluators


def test_evaluator_names_and_versions_are_unique() -> None:
    keys = [(e.name, e.version) for e in default_evaluators()]
    assert len(keys) == len(set(keys))


def test_every_layer_is_covered() -> None:
    assert {e.layer for e in default_evaluators()} == {
        "outcome",
        "policy",
        "trajectory",
        "response",
    }
