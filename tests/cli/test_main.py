import asyncio
from pathlib import Path

import pytest

from agent_eval.evaluation import completed
from evalctl.main import ERROR, HOLD, SHIP, main
from tests.conftest import LivePlatform

EVAL_FILE = """
from agent_eval import Eval
from examples.refund_agent.agent import CONFIG, RefundAgent
from examples.refund_agent.eval import DATASET, SCORERS
from tests.examples.test_refund_agent import scripted_gemini
from tests.sdk.fake_gemini import FakeGemini, reply

def careless(model, contents, config):
    # v2's bug: refunds straight away, without looking the order up.
    message = contents[0]["parts"][0]["text"]
    if "refund" in message.lower() and "ord_" in message and len(contents) == 1:
        order = message[message.index("ord_"):][:8]
        return reply(None, ("issue_refund", {{"order_id": order, "amount": 59.0}}))
    return scripted_gemini(model, contents, config)

brain = {brain}
config = {{**CONFIG, "prompt": CONFIG["prompt"] + "{suffix}"}}
Eval(
    agent="refund-agent",
    task=RefundAgent(FakeGemini(brain)).handle,
    dataset=DATASET,
    config=config,
    repetitions=2,
    scorers=SCORERS,
)
"""


@pytest.fixture
def platform_env(
    live_platform: LivePlatform,
    member_token: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    monkeypatch.setenv("AGENT_EVAL_URL", live_platform.url)
    monkeypatch.setenv("AGENT_EVAL_TOKEN", member_token[0])
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _file(folder: Path, name: str, brain: str, suffix: str) -> str:
    path = folder / name
    path.write_text(EVAL_FILE.format(brain=brain, suffix=suffix))
    return str(path)


async def test_evalctl_holds_a_version_that_broke_refunds(
    platform_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    v1 = _file(platform_env, "v1.py", "scripted_gemini", "")
    v2 = _file(platform_env, "v2.py", "careless", " Be quick.")
    # The first version has nothing to compare with and broke no rule: ship.
    assert await asyncio.to_thread(main, ["run", v1]) == SHIP
    assert "nothing to compare with" in capsys.readouterr().out
    first = completed[-1].experiment_id
    assert await asyncio.to_thread(main, ["run", v2]) == HOLD
    out = capsys.readouterr().out
    assert f"compared with experiment {first}" in out
    assert "Decision: HOLD" in out and "new hard failures: refund_after_lookup" in out
    assert "Cases that got worse" in out and "refund-delivered" in out
    assert "success rate" in out and "(real)" in out
    second = completed[-1].experiment_id
    # The same two experiments compared on their own give the same answer.
    assert await asyncio.to_thread(main, ["compare", first, second]) == HOLD
    # Compared with itself, nothing changed: ship.
    assert await asyncio.to_thread(main, ["compare", first, first]) == SHIP


async def test_a_file_without_an_eval_or_a_bad_token_is_an_error(
    platform_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = platform_env / "empty.py"
    empty.write_text("x = 1\n")
    assert await asyncio.to_thread(main, ["run", str(empty)]) == ERROR
    assert await asyncio.to_thread(main, ["--token", "aet_wrong", "compare", "a", "b"]) == ERROR
    assert "answered 401" in capsys.readouterr().err
