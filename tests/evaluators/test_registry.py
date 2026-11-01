from app.evaluators.registry import default_evaluators


def test_evaluator_names_and_versions_are_unique() -> None:
    keys = [(e.name, e.version) for e in default_evaluators()]
    assert len(keys) == len(set(keys))
    assert ("refund_outcome", "1.0.0") in keys
