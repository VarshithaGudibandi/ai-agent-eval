from app.metrics.comparison import ComparisonResult, Gates, SideFacts, compare, seed_for
from app.metrics.health import RunOutcome

SPEC = frozenset({"spec_1"})


def _facts(label: str, **changes: object) -> SideFacts:
    values: dict[str, object] = {
        "label": label,
        "dataset_version_id": "dv_1",
        "eval_spec_ids": SPEC,
        "finished": True,
        "repetitions": 3,
    }
    values.update(changes)
    return SideFacts(**values)  # type: ignore[arg-type]


def _side(
    passing: dict[str, int], reps: int = 3, failures: tuple[str, ...] = ()
) -> list[RunOutcome]:
    """Runs per case: the first `passing[case]` repetitions pass, the rest fail."""
    runs = []
    for case, passed in passing.items():
        for rep in range(reps):
            ok = rep < passed
            runs.append(
                RunOutcome(
                    run_id=f"{case}-{rep}",
                    case_key=case,
                    status="passed" if ok else "failed",
                    duration_ms=1000,
                    cost_micros=100,
                    cost_known=True,
                    hard_failures=() if ok else failures,
                )
            )
    return runs


CASES = [f"case-{n}" for n in range(12)]


def _compare(base: list[RunOutcome], cand: list[RunOutcome], **facts: object) -> ComparisonResult:
    return compare(
        base, cand, _facts("baseline"), _facts("candidate", **facts), Gates(), True, seed_for("t")
    )


def test_a_real_drop_holds_and_lists_the_flipped_cases_worst_first() -> None:
    base = _side(dict.fromkeys(CASES, 3))
    cand = _side(
        {c: (0 if n < 6 else 3) for n, c in enumerate(CASES)}, failures=("refund_after_lookup",)
    )
    result = _compare(base, cand)
    success = next(m for m in result.metrics if m.name == "success_rate")
    assert (success.delta, success.real, success.worse) == (-0.5, True, True)
    assert [c.outcome for c in result.cases[:6]] == ["worse"] * 6
    assert result.cases[-1].outcome == "same"
    assert result.decision == "hold"
    assert result.new_hard_failures == ["refund_after_lookup"]


def test_noise_is_not_a_regression() -> None:
    base = _side({c: (3 if n % 2 else 2) for n, c in enumerate(CASES)})
    cand = _side({c: (2 if n % 2 else 3) for n, c in enumerate(CASES)})
    result = _compare(base, cand)
    success = next(m for m in result.metrics if m.name == "success_rate")
    assert success.delta == 0 and success.real is False
    assert result.decision == "ship"


def test_pairing_sees_a_small_consistent_drop_through_case_difficulty() -> None:
    # Cases range from easy to hard. The candidate loses one repetition on four of them and
    # is the same elsewhere. Unpaired, case difficulty hides an 11-point drop; paired, every
    # case moved the same way or not at all, so the drop is real.
    difficulty = [3, 2, 1, 0] * 3
    base = _side(dict(zip(CASES, difficulty, strict=True)))
    worse = {CASES[n]: difficulty[n] - 1 for n in (0, 1, 4, 5)}
    cand = _side({**dict(zip(CASES, difficulty, strict=True)), **worse})
    success = next(m for m in _compare(base, cand).metrics if m.name == "success_rate")
    assert success.delta is not None and round(success.delta, 3) == -0.111
    assert success.real is True


def test_unfair_or_thin_comparisons_do_not_decide() -> None:
    base = _side(dict.fromkeys(CASES, 3))
    other_data = _compare(base, base, dataset_version_id="dv_2")
    assert other_data.decision == "insufficient_data"
    other_spec = _compare(base, base, eval_spec_ids=frozenset({"spec_2"}))
    assert "rescore one" in " ".join(other_spec.fairness)
    thin = _side(dict.fromkeys(CASES[:2], 3))
    assert _compare(thin, thin).decision == "insufficient_data"
    # A new hard failure holds even on thin data: it is a fact, not an estimate.
    broken = _side({CASES[0]: 3, CASES[1]: 2}, failures=("ran",))
    assert _compare(thin, broken).decision == "hold"


def test_the_same_inputs_give_the_same_intervals() -> None:
    base = _side({c: (3 if n % 3 else 1) for n, c in enumerate(CASES)})
    cand = _side({c: (2 if n % 2 else 3) for n, c in enumerate(CASES)})
    assert _compare(base, cand).metrics == _compare(base, cand).metrics
