from app.evaluators.coverage import Catch, published_coverage
from app.evaluators.registry import default_evaluators
from tests.evaluators.test_trace_mutations import ALTERNATIVES, MUTATIONS


def test_the_published_table_matches_the_mutation_suite() -> None:
    # Regenerate app/evaluators/coverage.json whenever this fails: an evaluator changed version
    # or the suite proves something new, and the published table must say so.
    coverage = published_coverage()
    evaluators = default_evaluators()
    assert [(e.name, e.version, e.layer) for e in coverage.evaluators] == [
        (e.name, e.version, e.layer) for e in evaluators
    ]
    for published in coverage.evaluators:
        proven = sorted(
            (
                Catch(mutation=m.name, dimension=dimension, reason_code=code)
                for m in MUTATIONS
                for (name, dimension), code in m.caught.items()
                if name == published.name
            ),
            key=lambda c: (c.mutation, c.dimension),
        )
        assert published.catches == proven, published.name
    assert coverage.valid_alternatives == sorted(a.name for a in ALTERNATIVES)


def test_every_evaluator_publishes_at_least_one_catch_and_the_gaps_are_named() -> None:
    coverage = published_coverage()
    assert all(e.catches for e in coverage.evaluators)
    assert coverage.not_covered
