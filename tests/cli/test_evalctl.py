import argparse
from typing import Any

import pytest

from cli.evalctl import ERROR, GATE_FAILED, PASSED, UNFINISHED, CommandError, main, parser, run


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def _card(config: str, passed: int, scorable: int, executed: int = 0) -> dict[str, Any]:
    return {
        "agent_config_id": config,
        "verified_success": _rate(passed, scorable),
        "coverage": _rate(scorable, scorable),
        "executed_violation_rate": _rate(executed, scorable),
        "unsafe_attempt_rate": _rate(0, scorable),
        "failure_reasons": [{"runs": 1, "reason_code": "MISSING_REFUND", "layer": "outcome"}],
    }


class StubApi:
    """Answers like the platform after an experiment finished, recording what was sent."""

    def __init__(self, status: str = "completed", cards: list[dict[str, Any]] | None = None):
        self.status = status
        self.cards = cards or [_card("cfg_v2", 9, 10)]
        self.sent: list[tuple[str, Any]] = []
        self.losses = 0

    def get(self, path: str, **params: Any) -> Any:
        if path == "/api/v1/agent-configs":
            return [
                {"id": "cfg_v1", "name": "agent", "created_at": "2026-11-01T00:00:00Z"},
                {"id": "cfg_v2", "name": "agent", "created_at": "2026-11-02T00:00:00Z"},
                {"id": "cfg_other", "name": "other", "created_at": "2026-11-01T00:00:00Z"},
            ]
        if path == "/api/v1/datasets":
            return [
                {
                    "name": "retail",
                    "versions": [{"id": "dv_1", "version": 1}, {"id": "dv_2", "version": 2}],
                }
            ]
        return {"id": "exp_1", "status": self.status, "scorecards": self.cards}

    def post(self, path: str, body: Any = None, headers: Any = None) -> Any:
        self.sent.append((path, body))
        if path == "/api/v1/comparisons":
            tasks = [
                {
                    "task_key": "refund-a",
                    "outcome": "loss",
                    "baseline": {"passed": 2, "scorable": 2},
                    "candidate": {"passed": 0, "scorable": 2, "reason_codes": ["MISSING_REFUND"]},
                }
            ][: self.losses]
            return {
                "id": "cmp_1",
                "result": {
                    "losses": self.losses,
                    "wins": 0,
                    "ties": 3,
                    "tasks": tasks,
                    "notes": [],
                },
            }
        return {"id": "exp_1"}


def _args(*extra: str) -> argparse.Namespace:
    return parser().parse_args(
        ["run", "--config", "agent", "--dataset", "retail", "--wait", "--poll", "0", *extra]
    )


def test_the_newest_config_and_dataset_version_are_used() -> None:
    api = StubApi()
    assert run(_args(), api) == PASSED
    path, body = api.sent[0]
    assert path == "/api/v1/experiments"
    assert (body["agent_config_ids"], body["dataset_version_id"]) == (["cfg_v2"], "dv_2")
    assert (body["split"], body["mode"]) == ("validation", "real")


def test_low_success_fails_the_gate(capsys: pytest.CaptureFixture[str]) -> None:
    api = StubApi(cards=[_card("cfg_v2", 7, 10)])
    assert run(_args("--min-success", "0.8"), api) == GATE_FAILED
    assert "FAIL verified success" in capsys.readouterr().out


def test_nothing_scorable_never_passes() -> None:
    api = StubApi(cards=[_card("cfg_v2", 0, 0)])
    assert run(_args("--min-success", "0"), api) == GATE_FAILED


def test_any_executed_violation_fails_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    api = StubApi(cards=[_card("cfg_v2", 10, 10, executed=1)])
    assert run(_args(), api) == GATE_FAILED
    assert "FAIL executed violations" in capsys.readouterr().out
    assert run(_args("--max-executed-violations", "1"), api) == PASSED


def test_regressions_against_the_baseline_are_listed_and_can_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = StubApi(cards=[_card("cfg_other", 10, 10), _card("cfg_v2", 9, 10)])
    api.losses = 1
    code = run(_args("--baseline", "other", "--max-regressions", "0"), api)
    out = capsys.readouterr().out
    assert code == GATE_FAILED
    assert "regressed refund-a: 2/2 -> 0/2 (MISSING_REFUND)" in out
    assert "FAIL regressions" in out
    [(_, experiment), (_, comparison)] = api.sent
    assert experiment["agent_config_ids"] == ["cfg_other", "cfg_v2"]
    assert comparison["candidate"]["agent_config_id"] == "cfg_v2"
    # Listing regressions does not block unless a limit is set.
    assert run(_args("--baseline", "other"), api) == PASSED


@pytest.mark.parametrize("status", ["paused_budget", "running"])
def test_an_unfinished_experiment_is_its_own_exit_code(status: str) -> None:
    api = StubApi(status=status)
    assert run(_args("--timeout", "0"), api) == UNFINISHED


def test_mistakes_are_reported_without_a_traceback() -> None:
    with pytest.raises(CommandError, match="no agent config named"):
        run(parser().parse_args(["run", "--config", "nope", "--dataset", "retail"]), StubApi())
    with pytest.raises(CommandError, match="no dataset named"):
        run(parser().parse_args(["run", "--config", "agent", "--dataset", "nope"]), StubApi())


def test_a_missing_token_is_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("EVALCTL_TOKEN", raising=False)
    assert main(["compare", "exp_a", "exp_b"]) == ERROR
    assert "EVALCTL_TOKEN" in capsys.readouterr().err
