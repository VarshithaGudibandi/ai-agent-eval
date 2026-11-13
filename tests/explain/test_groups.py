from app.explain.groups import RunFailures, group_failures


def _run(
    n: int, case: str, *failed: str, questions: dict[str, tuple[str, ...]] | None = None
) -> RunFailures:
    return RunFailures(f"r{n}", case, failed, {k: f"{k} failed" for k in failed}, questions or {})


def test_groups_are_the_failures_that_grew_biggest_first() -> None:
    baseline = [_run(1, "a", "tone"), _run(2, "b")]
    candidate = [
        _run(3, "a", "tone"),
        _run(4, "b", "lookup_first", "tone"),
        _run(5, "c", "lookup_first", "judge", questions={"judge": ("polite",)}),
    ]
    groups = group_failures(baseline, candidate, worse_cases=None)
    assert [(g.key, g.count, g.baseline_count) for g in groups] == [
        ("lookup_first", 2, 0),
        ("judge:polite", 1, 0),
        ("tone", 2, 1),
    ]
    assert groups[0].key == "lookup_first" and groups[0].run_ids == ("r4", "r5")
    assert groups[0].reasons == ("lookup_first failed",)


def test_a_paired_comparison_only_blames_cases_that_got_worse() -> None:
    baseline = [_run(1, "a", "tone"), _run(2, "b")]
    candidate = [_run(3, "a", "tone"), _run(4, "b", "lookup_first")]
    groups = group_failures(baseline, candidate, worse_cases={"b"})
    assert [g.key for g in groups] == ["lookup_first"]
