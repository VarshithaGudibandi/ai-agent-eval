import json
from collections.abc import Callable

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select

from app.db import models
from app.ingest.redact import MASK
from tests.api.conftest import Caller, SignIn, token_caller
from tests.ingest.sample_traces import EXPECTED_COST, LEAKED_KEY, PRICE, support_trace


async def _count(app: FastAPI, model: type[models.Base], workspace_id: str) -> int:
    async with app.state.sessionmaker() as session:
        total = await session.scalar(
            select(func.count()).select_from(model).where(model.workspace_id == workspace_id)  # type: ignore[attr-defined]
        )
    return int(total or 0)


async def _post(caller: Caller, *traces: dict[str, object]) -> httpx.Response:
    return await caller.post("/api/v1/traces", json={"traces": list(traces)})


async def test_a_trace_sent_twice_is_stored_once_with_its_tree_cost_and_version(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    assert (await owner.post("/api/v1/price-versions", json=PRICE)).status_code == 201
    first = await _post(owner, support_trace())
    again = await _post(owner, support_trace())
    assert first.status_code == again.status_code == 200
    assert first.json() == {"accepted": ["trace-001"], "duplicates": [], "rejected": []}
    assert again.json() == {"accepted": [], "duplicates": ["trace-001"], "rejected": []}

    ws = owner.workspace_id
    for model in (models.Trace, models.Run, models.Agent, models.AgentVersion):
        assert await _count(app, model, ws) == 1, model.__tablename__
    assert await _count(app, models.Span, ws) == 4

    async with app.state.sessionmaker() as session:
        trace = await session.scalar(select(models.Trace).where(models.Trace.workspace_id == ws))
        run = await session.scalar(select(models.Run).where(models.Run.workspace_id == ws))
        version = await session.scalar(
            select(models.AgentVersion).where(models.AgentVersion.workspace_id == ws)
        )
        spans = {
            s.span_id: s
            for s in await session.scalars(
                select(models.Span).where(models.Span.workspace_id == ws)
            )
        }
    assert trace is not None and run is not None and version is not None
    assert trace.status == "normalized"
    # The tree: one root, every other span under it.
    assert spans["root"].parent_span_id is None
    assert {s.parent_span_id for k, s in spans.items() if k != "root"} == {"root"}
    assert (spans["m1"].kind, spans["t1"].kind, spans["root"].kind) == ("model", "tool", "step")
    assert spans["m1"].attributes["time_to_first_token_ms"] == 300
    # The cost, from the workspace's price for the model.
    assert (run.cost_micros, run.cost_known) == (EXPECTED_COST, True)
    assert (spans["m1"].attributes["cost_micros"], spans["m2"].attributes["cost_micros"]) == (
        1300,
        1620,
    )
    assert (run.input_tokens, run.output_tokens, run.duration_ms) == (2700, 80, 2500)
    assert (run.environment, run.session_id) == ("production", "sess-42")
    assert run.agent_version_id == version.id
    assert (version.label, len(version.fingerprint)) == ("v1", 64)


async def test_secrets_are_masked_before_anything_is_stored(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    assert (await _post(owner, support_trace())).json()["accepted"] == ["trace-001"]
    async with app.state.sessionmaker() as session:
        trace = await session.scalar(
            select(models.Trace).where(models.Trace.workspace_id == owner.workspace_id)
        )
        tool = await session.scalar(
            select(models.Span).where(
                models.Span.workspace_id == owner.workspace_id, models.Span.span_id == "t1"
            )
        )
    assert trace is not None and tool is not None
    stored = json.dumps(trace.payload) + json.dumps(tool.output)
    assert LEAKED_KEY not in stored
    assert tool.output["debug"] == f"called with key={MASK}"


async def test_a_new_config_is_a_new_version_of_the_same_agent(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    changed = {**support_trace()["config"], "prompt": "Refund only with approval. Be brief."}  # type: ignore[dict-item]
    response = await _post(
        owner,
        support_trace("t-a"),
        support_trace("t-b"),
        support_trace("t-c", config=changed, label="v2"),
    )
    assert response.json()["accepted"] == ["t-a", "t-b", "t-c"]
    assert await _count(app, models.Agent, owner.workspace_id) == 1
    async with app.state.sessionmaker() as session:
        labels = set(
            await session.scalars(
                select(models.AgentVersion.label).where(
                    models.AgentVersion.workspace_id == owner.workspace_id
                )
            )
        )
    assert labels == {"v1", "v2"}


async def test_without_a_price_the_cost_is_marked_unknown(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    await _post(owner, support_trace())
    async with app.state.sessionmaker() as session:
        run = await session.scalar(
            select(models.Run).where(models.Run.workspace_id == owner.workspace_id)
        )
    assert run is not None and (run.cost_micros, run.cost_known) == (0, False)


async def test_bad_traces_are_rejected_one_by_one(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    good = support_trace("good")
    two_roots = support_trace("two-roots")
    two_roots["spans"][1]["parent_id"] = None  # type: ignore[index]
    response = await _post(owner, good, two_roots, {"trace_id": "no-spans", "agent": "a"})
    body = response.json()
    assert body["accepted"] == ["good"]
    assert [r["trace_id"] for r in body["rejected"]] == ["two-roots", "no-spans"]
    assert "exactly one root" in body["rejected"][0]["error"]
    assert await _count(app, models.Trace, owner.workspace_id) == 1


async def test_a_secret_in_a_rejected_trace_is_not_echoed_back(new_owner: SignIn) -> None:
    owner = await new_owner()
    broken = support_trace("leaky", agent=LEAKED_KEY, spans=[])
    body = (await _post(owner, broken)).json()
    assert LEAKED_KEY not in json.dumps(body)


async def test_the_batch_itself_must_be_valid_and_not_too_large(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    empty = await owner.post("/api/v1/traces", json={"traces": []})
    assert (empty.status_code, empty.json()["error"]) == (422, "batch_invalid")
    limit = app.state.settings.ingest_max_bytes
    huge = support_trace(spans=[{**support_trace()["spans"][0], "input": "x" * limit}])  # type: ignore[index]
    response = await _post(owner, huge)
    assert (response.status_code, response.json()["error"]) == (413, "batch_too_large")
    assert await _count(app, models.Trace, owner.workspace_id) == 0


async def test_viewers_cannot_send_traces_but_member_tokens_can(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    viewer = await token_caller(owner, clients(), role="viewer")
    member = await token_caller(owner, clients(), role="member")
    assert (await _post(viewer, support_trace())).status_code == 403
    assert (await _post(member, support_trace())).json()["accepted"] == ["trace-001"]
