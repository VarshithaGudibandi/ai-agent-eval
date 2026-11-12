from dataclasses import replace

import pytest

from app.reports.comparison import (
    MIN_TASKS,
    IncompatibleError,
    NotFinishedError,
    Side,
    bootstrap,
    compare,
    pair_tasks,
)
from app.reports.metrics import RunFacts, ScoreFacts

BASE = Side(
    experiment_id="exp_1",
    agent_config_id="cfg_v1",
    config_name="v1",
    mode="real",
    status="completed",
    dataset_version_id="dv_1",
    split="validation",
    environments=["support-retail@1"],
    evaluators=[{"name": "refund_outcome", "version": "1.1.0"}],
    limits={"max_turns": 12},
)
CAND = replace(BASE, agent_config_id="cfg_v2", config_name="v2")


def _run(config: str, task: str, status: str, repetition: int = 1, **kw: object) -> RunFacts:
    reason = ScoreFacts("e", "1", "outcome", "refund_amount", True, "fail", "WRONG", {})
    return RunFacts(
        run_id=f"{config}_{task}_{repetition}",
        agent_config_id=config,
        task_version_id=f"tv_{task}",
        task_key=task,
        category="refund",
        repetition=repetition,
        execution_status="completed",
        evaluation_status=status,
        attempt_id="att",
        scores=[reason] if status == "fail" else [],
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "demo"),
        ("dataset_version_id", "dv_2"),
        ("split", "dev"),
        ("environments", ["support-retail@2"]),
        ("evaluators", [{"name": "refund_outcome", "version": "1.2.0"}]),
        ("limits", {"max_turns": 20}),
    ],
)
def test_only_the_config_may_differ_and_the_difference_is_named(field: str, value: object) -> None:
    with pytest.raises(IncompatibleError) as caught:
        compare(BASE, replace(CAND, **{field: value}), [], [])
    assert caught.value.field == field


def test_a_config_is_not_compared_with_itself() -> None:
    with pytest.raises(IncompatibleError):
        compare(BASE, BASE, [], [])


@pytest.mark.parametrize("status", ["queued", "running", "paused_budget", "paused_user"])
def test_unfinished_experiments_are_not_compared(status: str) -> None:
    with pytest.raises(NotFinishedError):
        compare(BASE, replace(CAND, status=status), [], [])


def test_tasks_pair_with_their_repetitions_together() -> None:
    base = [
        _run("cfg_v1", "a", "pass", 1),
        _run("cfg_v1", "a", "pass", 2),
        _run("cfg_v1", "b", "pass", 1),
        _run("cfg_v1", "b", "fail", 2),
        _run("cfg_v1", "c", "pass"),
        _run("cfg_v1", "d", "pass"),
    ]
    cand = [
        _run("cfg_v2", "a", "pass", 1),
        _run("cfg_v2", "a", "fail", 2),
        _run("cfg_v2", "b", "pass", 1),
        _run("cfg_v2", "b", "pass", 2),
        _run("cfg_v2", "c", "pass"),
        _run("cfg_v2", "d", "pending"),
    ]
    tasks = {t.task_key: t for t in pair_tasks(base, cand)}
    assert (tasks["a"].outcome, tasks["a"].delta) == ("loss", -0.5)
    assert tasks["a"].candidate.run_ids == ["cfg_v2_a_1", "cfg_v2_a_2"]
    assert tasks["a"].candidate.reason_codes == ["WRONG"]
    assert (tasks["b"].outcome, tasks["b"].delta) == ("win", 0.5)
    assert tasks["c"].outcome == "tie"
    # A side with nothing scorable cannot be paired, and is never read as a loss.
    assert (tasks["d"].outcome, tasks["d"].delta) == ("unscored", None)


def test_regressions_come_first_and_worst_first() -> None:
    base = [_run("cfg_v1", t, "pass", r) for t in "abcd" for r in (1, 2)]
    cand = [
        _run("cfg_v2", "a", "pass", 1),
        _run("cfg_v2", "a", "fail", 2),
        _run("cfg_v2", "b", "fail", 1),
        _run("cfg_v2", "b", "fail", 2),
        _run("cfg_v2", "c", "pass", 1),
        _run("cfg_v2", "c", "pass", 2),
        _run("cfg_v2", "d", "fail", 1),
        _run("cfg_v2", "d", "fail", 2),
    ]
    order = [(t.task_key, t.outcome) for t in pair_tasks(base, cand)]
    assert order == [("b", "loss"), ("d", "loss"), ("a", "loss"), ("c", "tie")]


def test_a_regression_is_reported_with_counts_and_a_noise_warning() -> None:
    base = [_run("cfg_v1", t, "pass") for t in "abc"]
    cand = [_run("cfg_v2", "a", "pass"), _run("cfg_v2", "b", "fail"), _run("cfg_v2", "c", "pass")]
    result = compare(BASE, CAND, base, cand)
    assert (result.wins, result.losses, result.ties, result.unscored) == (0, 1, 2, 0)
    assert result.tasks[0].task_key == "b"
    assert result.success.delta == pytest.approx(-1 / 3)
    assert result.may_be_noise
    assert any(f"fewer than {MIN_TASKS}" in n for n in result.notes)
    assert result.baseline.scorecard.verified_success.value == 1.0


def test_the_interval_is_reproducible_and_brackets_the_estimate() -> None:
    pairs = [(1.0, 1.0)] * 20 + [(1.0, 0.0)] * 10 + [(0.0, 1.0)] * 5
    first, again = bootstrap(pairs, seed=7), bootstrap(pairs, seed=7)
    assert first == again
    assert first.low is not None and first.high is not None and first.delta is not None
    assert first.low <= first.delta <= first.high
    assert first.low < 0


def test_a_clear_difference_on_enough_tasks_is_not_called_noise() -> None:
    tasks = [f"t{i:02}" for i in range(40)]
    base = [_run("cfg_v1", t, "pass") for t in tasks]
    cand = [_run("cfg_v2", t, "fail" if i < 20 else "pass") for i, t in enumerate(tasks)]
    result = compare(BASE, CAND, base, cand)
    assert result.success.high is not None and result.success.high < 0
    assert not result.may_be_noise


def test_a_moved_model_alias_is_called_out() -> None:
    base = [_run("cfg_v1", "a", "pass", resolved_models={"m-1", "m-2"})]
    cand = [_run("cfg_v2", "a", "pass", resolved_models={"m-2"})]
    notes = compare(BASE, CAND, base, cand).notes
    assert any("more than one model version" in n for n in notes)
    assert any("different model versions" in n for n in notes)


def test_no_paired_tasks_gives_no_interval() -> None:
    result = compare(BASE, CAND, [_run("cfg_v1", "a", "pending")], [_run("cfg_v2", "a", "pass")])
    assert result.paired_tasks == 0
    assert result.success.delta is None and result.may_be_noise
