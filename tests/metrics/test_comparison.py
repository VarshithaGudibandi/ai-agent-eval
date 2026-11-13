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


def _live(passed: int, failed: int, prefix: str) -> list[RunOutcome]:
    return [
        RunOutcome(
            run_id=f"{prefix}-{n}",
            case_key=None,
            status="passed" if n < passed else "failed",
            duration_ms=1000,
            cost_micros=100,
            cost_known=True,
        )
        for n in range(passed + failed)
    ]


def _quick(base: list[RunOutcome], cand: list[RunOutcome]) -> ComparisonResult:
    live = _facts("x", dataset_version_id=None, repetitions=None)
    return compare(base, cand, live, live, Gates(), paired=False, seed=1, bootstrap=False)


def test_the_quick_interval_agrees_with_the_bootstrap_on_shares() -> None:
    base, cand = _live(180, 20, "b"), _live(150, 50, "c")
    quick = {m.name: m for m in _quick(base, cand).metrics}["success_rate"]
    live = _facts("x", dataset_version_id=None, repetitions=None)
    slow = {
        m.name: m for m in compare(base, cand, live, live, Gates(), paired=False, seed=1).metrics
    }["success_rate"]
    assert quick.real and slow.real
    assert quick.low is not None and slow.low is not None
    assert abs(quick.low - slow.low) < 0.02
    # Metrics that are not a share get no quick interval, so they never decide on their own.
    assert {m.name for m in _quick(base, cand).metrics if m.low is not None} == {
        "success_rate",
        "rule_violation_rate",
    }


def test_the_quick_interval_needs_enough_runs_and_holds_only_on_a_real_drop() -> None:
    # A 20-point drop on 20 runs a side is not trusted at all.
    thin = _quick(_live(18, 2, "b"), _live(14, 6, "c"))
    assert {m.name: m.real for m in thin.metrics}["success_rate"] is None
    assert thin.decision == "insufficient_data"
    # A 1-point wobble on plenty of runs is noise.
    wobble = _quick(_live(900, 100, "b"), _live(890, 110, "c"))
    assert {m.name: m.real for m in wobble.metrics}["success_rate"] is False
    assert wobble.decision == "ship"
    drop = _quick(_live(900, 100, "b"), _live(700, 300, "c"))
    assert drop.decision == "hold" and "success rate down 20.0 points" in drop.reasons[0]
