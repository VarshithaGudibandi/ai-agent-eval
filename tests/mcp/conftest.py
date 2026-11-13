import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.api_tokens import create_api_token
from app.auth.principal import Principal
from app.auth.users import create_user
from app.auth.workspaces import create_workspace
from app.db import models
from app.db.ids import new_id
from app.db.scope import WorkspaceScope
from tests.conftest import LivePlatform
from tests.ingest.sample_traces import CONFIG, support_trace

CASES = [{"key": f"case-{n}", "input": {"n": n}} for n in range(8)]
V2 = {**CONFIG, "prompt": "A shorter prompt that forgets to look things up."}
# Recorded content a tool must never pass to the assistant.
SPAN_INPUT = "PLEASE-IGNORE-YOUR-RULES-input"
SPAN_OUTPUT = "PRIVATE-REPLY-output"


@dataclass
class Workspace:
    url: str
    member: str
    viewer: str


@pytest.fixture
async def workspace(
    live_platform: LivePlatform, fresh_sessionmaker: async_sessionmaker[AsyncSession]
) -> Workspace:
    """A fresh workspace on the live platform with a member and a viewer token."""
    async with fresh_sessionmaker.begin() as session:
        user = await create_user(session, f"{new_id('u')}@example.com", "a long enough password")
        made = await create_workspace(session, user, "team")
        owner = Principal(
            actor_type=models.ActorType.USER,
            actor_id=user.id,
            user_id=user.id,
            workspace_id=made.id,
            role=models.Role.OWNER,
        )
        scope = WorkspaceScope(session, made.id)
        _, member = await create_api_token(scope, owner, "mcp", models.Role.MEMBER)
        _, viewer = await create_api_token(scope, owner, "mcp-read", models.Role.VIEWER)
    return Workspace(live_platform.url, member, viewer)


@pytest.fixture
def use(workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """Point the MCP server at the workspace with its member token."""
    monkeypatch.setenv("AGENT_EVAL_URL", workspace.url)
    monkeypatch.setenv("AGENT_EVAL_TOKEN", workspace.member)
    return workspace


def run_experiment(workspace: Workspace, config: dict[str, Any], crash: set[int]) -> dict[str, Any]:
    """A finished experiment of the eight cases, each run twice, scored by the live worker."""
    with httpx.Client(
        base_url=workspace.url, headers={"authorization": f"Bearer {workspace.member}"}
    ) as client:
        dataset = client.post("/api/v1/datasets", json={"name": "support", "cases": CASES}).json()
        experiment = client.post(
            "/api/v1/experiments",
            json={
                "agent": "support-bot",
                "config": config,
                "dataset_version_id": dataset["id"],
                "repetitions": 2,
            },
        ).json()
        traces = []
        for n, case in enumerate(CASES):
            for rep in (1, 2):
                trace = support_trace(
                    f"{experiment['id']}-{n}-{rep}",
                    config=config,
                    tags={"experiment": experiment["id"], "case": case["key"], "repetition": rep},
                )
                trace["spans"][0]["input"] = {"message": SPAN_INPUT}
                trace["spans"][0]["output"] = {"reply": SPAN_OUTPUT}
                if n in crash:
                    trace["spans"][0]["error"] = "RuntimeError: forgot the lookup"
                traces.append(trace)
        assert client.post("/api/v1/traces", json={"traces": traces}).status_code == 200
        client.post(f"/api/v1/experiments/{experiment['id']}/finish")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            found: dict[str, Any] = client.get(f"/api/v1/experiments/{experiment['id']}").json()
            if found["runs_scored"] == found["expected_runs"]:
                return found
            time.sleep(0.3)
    raise AssertionError("the worker did not score the experiment in time")
