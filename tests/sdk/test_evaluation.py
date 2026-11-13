import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_eval import Eval, flush, save_eval_spec, traced
from agent_eval.evaluation import judge_cap
from app.db import models
from tests.conftest import LivePlatform

CASES = [
    {"key": "late-1", "input": {"message": "Where is ord_1?"}, "expected": {"reply": "shipped"}},
    {"key": "late-2", "input": {"message": "Where is ord_2?"}, "tags": {"category": "late"}},
    {"key": "fail-1", "input": {"message": "crash"}},
]
CONFIG = {"model": "gemini-3.5-flash-lite", "prompt": "You track orders."}


@traced
def lookup(message: str) -> str:
    if message == "crash":
        raise RuntimeError("lookup broke")
    return "shipped"


@traced
def answer(case_input: dict[str, Any]) -> str:
    return f"It {lookup(case_input['message'])}."


def _dataset(tmp_path: Path, cases: list[dict[str, Any]] = CASES) -> Path:
    path = tmp_path / "orders.jsonl"
    path.write_text("".join(json.dumps(c) + "\n" for c in cases))
    return path


async def _eval(**options: Any) -> Eval:
    return await asyncio.to_thread(Eval, **options)


async def _runs(
    fresh_sessionmaker: async_sessionmaker[AsyncSession], experiment_id: str
) -> list[Any]:
    async with fresh_sessionmaker() as session:
        rows = await session.execute(
            select(models.DatasetCase.case_key, models.Run.repetition)
            .join(models.DatasetCase, models.DatasetCase.id == models.Run.case_id)
            .where(models.Run.experiment_id == experiment_id)
        )
        return sorted(rows.all())


async def test_eval_sends_one_run_per_case_and_repetition(
    live_platform: LivePlatform,
    member_token: tuple[str, str],
    fresh_sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token, _ = member_token
    options = {
        "agent": "order-bot",
        "task": answer,
        "dataset": _dataset(tmp_path),
        "config": CONFIG,
        "repetitions": 2,
        "url": live_platform.url,
        "token": token,
        "pending_dir": tmp_path / "pending",
    }
    first = await _eval(**options)
    assert (first.expected_runs, first.runs_received, first.task_errors) == (6, 6, 2)
    # The crash starts in a tool, so both built-ins catch it, on both repetitions.
    assert (first.runs_scored, first.runs_passed) == (6, 4)
    assert first.failures == {"run_completed": 2, "tool_errors": 2}
    assert await _runs(fresh_sessionmaker, first.experiment_id) == [
        (key, rep) for key in ("fail-1", "late-1", "late-2") for rep in (1, 2)
    ]
    async with httpx.AsyncClient(headers={"authorization": f"Bearer {token}"}) as client:
        experiment = (
            await client.get(f"{live_platform.url}/api/v1/experiments/{first.experiment_id}")
        ).json()
    assert experiment["status"] == "finished"
    out = capsys.readouterr().out
    assert "6 of 6 runs received, 2 raised an error" in out
    assert "4 of 6 runs passed" in out and "run_completed failed in 2 runs" in out
    # Running the same file again makes a new experiment on the same versions.
    second = await _eval(**options, label=None)
    assert second.experiment_id != first.experiment_id
    async with fresh_sessionmaker() as session:
        rows = await session.execute(
            select(models.Experiment.dataset_version_id, models.Experiment.agent_version_id).where(
                models.Experiment.id.in_([first.experiment_id, second.experiment_id])
            )
        )
        assert len(set(rows.all())) == 1


async def test_an_async_task_and_a_platform_dataset_by_name(
    live_platform: LivePlatform, member_token: tuple[str, str], tmp_path: Path
) -> None:
    token, _ = member_token
    async with httpx.AsyncClient(headers={"authorization": f"Bearer {token}"}) as client:
        created = await client.post(
            f"{live_platform.url}/api/v1/datasets",
            json={"name": "shared-orders", "cases": CASES[:2]},
        )
    assert created.status_code == 201

    @traced
    async def async_answer(case_input: dict[str, Any]) -> str:
        await asyncio.sleep(0)
        return "ok"

    result = await _eval(
        agent="order-bot",
        task=async_answer,
        dataset="shared-orders",
        config=CONFIG,
        repetitions=1,
        url=live_platform.url,
        token=token,
        pending_dir=tmp_path / "pending",
    )
    assert (result.runs_received, result.task_errors) == (2, 0)
    with pytest.raises(ValueError, match="no dataset file or platform dataset"):
        await _eval(
            agent="a",
            task=answer,
            dataset="missing",
            config={},
            url=live_platform.url,
            token=token,
            pending_dir=tmp_path / "pending",
        )


async def test_stopping_the_server_mid_run_loses_nothing(
    live_platform: LivePlatform,
    member_token: tuple[str, str],
    fresh_sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token, _ = member_token
    calls = {"n": 0}

    @traced
    def flaky_world(case_input: dict[str, Any]) -> str:
        calls["n"] += 1
        if calls["n"] == 3:
            live_platform.stop()
        return "done"

    pending = tmp_path / "pending"
    result = await _eval(
        agent="order-bot",
        task=flaky_world,
        dataset=_dataset(tmp_path),
        config=CONFIG,
        repetitions=2,
        url=live_platform.url,
        token=token,
        pending_dir=pending,
    )
    assert result.saved == 4
    lines = (pending / f"{result.experiment_id}.jsonl").read_text().splitlines()
    saved = [json.loads(line) for line in lines]
    assert len(saved) == 4
    assert "Run agent_eval.flush() once the platform is back" in capsys.readouterr().out
    live_platform.start()
    uploaded = await asyncio.to_thread(flush, live_platform.url, token, pending)
    assert (uploaded.uploaded, uploaded.rejected, uploaded.remaining) == (4, 0, 0)
    assert not (pending / f"{result.experiment_id}.jsonl").exists()
    runs = await _runs(fresh_sessionmaker, result.experiment_id)
    assert runs == [(key, rep) for key in ("fail-1", "late-1", "late-2") for rep in (1, 2)]
    # A second flush finds nothing left to send.
    again = await asyncio.to_thread(flush, live_platform.url, token, pending)
    assert again.uploaded == 0
    async with fresh_sessionmaker() as session:
        status = await session.scalar(
            select(models.Experiment.status).where(models.Experiment.id == result.experiment_id)
        )
        traces = await session.scalar(
            select(func.count())
            .select_from(models.Run)
            .where(models.Run.experiment_id == result.experiment_id)
        )
    assert (status, traces) == ("finished", 6)


def test_a_case_file_must_name_each_case(tmp_path: Path) -> None:
    path = _dataset(tmp_path, [{"input": "no key"}])
    with pytest.raises(ValueError, match=r"case 1 .* needs a key and an input"):
        Eval(
            agent="a",
            task=answer,
            dataset=path,
            config={},
            token="aet_fake",
            url="http://127.0.0.1:1",
            pending_dir=tmp_path / "pending",
        )


async def test_saving_a_spec_for_an_agent_never_seen_says_what_to_do(
    live_platform: LivePlatform, member_token: tuple[str, str]
) -> None:
    token, _ = member_token
    with pytest.raises(ValueError, match="send it a trace or run an Eval first"):
        await asyncio.to_thread(
            save_eval_spec,
            "nobody",
            [{"key": "ran", "check": "run_completed"}],
            live_platform.url,
            token,
        )


async def test_a_judge_that_cannot_run_is_reported_not_counted_as_a_failure(
    live_platform: LivePlatform,
    member_token: tuple[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token, _ = member_token
    result = await _eval(
        agent="order-bot",
        task=answer,
        dataset=_dataset(tmp_path, CASES[:2]),
        config=CONFIG,
        repetitions=1,
        url=live_platform.url,
        token=token,
        pending_dir=tmp_path / "pending",
        scorers=[{"key": "support", "check": "checklist", "params": {"pack": "support"}}],
    )
    # The workspace has no Gemini key, so the judge grades nothing and says why.
    assert (result.runs_scored, result.runs_passed, result.failures) == (2, 0, {})
    assert result.errors == {"support": 2}
    assert "support could not grade 2 runs" in capsys.readouterr().out


def test_the_lower_judge_cap_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_EVAL_MAX_JUDGE_CALLS", raising=False)
    assert (judge_cap(None), judge_cap(30)) == (None, 30)
    monkeypatch.setenv("AGENT_EVAL_MAX_JUDGE_CALLS", "20")
    assert (judge_cap(None), judge_cap(30), judge_cap(5)) == (20, 20, 5)
    monkeypatch.setenv("AGENT_EVAL_MAX_JUDGE_CALLS", "lots")
    with pytest.raises(ValueError, match="whole number"):
        judge_cap(None)
