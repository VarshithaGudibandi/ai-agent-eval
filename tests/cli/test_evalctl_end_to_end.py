"""evalctl against the real API and a real worker, the way a CI pipeline uses it."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from app.api.main import create_app
from app.execution.worker import run as run_worker
from app.settings import Settings
from cli.evalctl import GATE_FAILED, PASSED, main
from tests.conftest import GOOD_SCRIPT, TASK_FILE, UNAPPROVED_SCRIPT


@pytest.fixture
async def platform_url(settings: Settings, fresh_database_url: str) -> AsyncIterator[str]:
    """The API on a local port with one worker, on a database of its own."""
    fast = settings.model_copy(
        update={"database_url": fresh_database_url, "worker_idle_seconds": 0.05}
    )
    server = uvicorn.Server(
        uvicorn.Config(create_app(fast), host="127.0.0.1", port=0, log_level="warning")
    )
    serving = asyncio.create_task(server.serve())
    stop = asyncio.Event()
    working = asyncio.create_task(run_worker(fast, stop))
    while not server.started:  # noqa: ASYNC110 - uvicorn exposes no startup event
        await asyncio.sleep(0.02)
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    stop.set()
    server.should_exit = True
    await asyncio.gather(working, serving)


@pytest.fixture
async def ci_env(platform_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An owner, a member token for CI, and the task and agent configs on disk."""
    async with httpx.AsyncClient(base_url=platform_url) as client:
        login = {"email": "ci@example.com", "password": "a long enough password"}
        await client.post("/api/v1/auth/setup", json={**login, "workspace_name": "CI"})
        csrf = (await client.post("/api/v1/auth/login", json=login)).json()["csrf_token"]
        token = await client.post(
            "/api/v1/api-tokens",
            json={"name": "ci", "role": "member"},
            headers={"x-csrf-token": csrf},
        )
    monkeypatch.setenv("EVALCTL_URL", platform_url)
    monkeypatch.setenv("EVALCTL_TOKEN", token.json()["token"])
    (tmp_path / "tasks").mkdir()
    task = json.loads(TASK_FILE.read_text())
    (tmp_path / "tasks" / "refund.json").write_text(json.dumps({**task, "split": "dev"}))
    for name, script in (("v1", GOOD_SCRIPT), ("v2", UNAPPROVED_SCRIPT)):
        (tmp_path / f"{name}.json").write_text(json.dumps({"script": script}))
    return tmp_path


async def _evalctl(*argv: str) -> int:
    # evalctl blocks while it waits, so it runs off the loop that serves the API.
    return await asyncio.to_thread(main, list(argv))


async def test_a_pipeline_imports_registers_runs_and_passes(
    ci_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await _evalctl("import", str(ci_env), "--name", "retail") == PASSED
    for name in ("v1", "v2"):
        settings = str(ci_env / f"{name}.json")
        assert (
            await _evalctl(
                "config", "--name", name, "--adapter", "scripted", "--settings-file", settings
            )
            == PASSED
        )
    run = ("run", "--dataset", "retail", "--split", "dev", "--mode", "demo", "--wait")
    passed = await _evalctl(*run, "--config", "v1", "--poll", "0.1", "--min-success", "1")
    out = capsys.readouterr().out
    assert passed == PASSED, out
    assert "PASS verified success" in out

    # v2 skips approval. Against v1 it regresses, and the regression blocks the deploy.
    report = ci_env / "result.json"
    blocked = await _evalctl(
        *run,
        "--config", "v2",
        "--baseline", "v1",
        "--poll", "0.1",
        "--min-success", "0",
        "--max-regressions", "0",
        "--json", str(report),
    )  # fmt: skip
    out = capsys.readouterr().out
    assert blocked == GATE_FAILED, out
    assert "regressed refund-damaged-headphones: 1/1 -> 0/1" in out
    assert "FAIL regressions" in out
    result = json.loads(report.read_text())
    assert [g["passed"] for g in result["gates"]] == [True, True, False]

    exported = ci_env / "report.json"
    code = await _evalctl("export", result["experiment_id"], "-o", str(exported))
    # Demo results never leave as a report, and evalctl says so instead of crashing.
    assert code != PASSED
    assert "demo_not_exportable" in capsys.readouterr().err
    assert not exported.exists()
