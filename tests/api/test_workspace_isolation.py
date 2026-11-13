"""Workspace A tries to reach workspace B through every route. Every attempt must fail.

Rows in another workspace must answer exactly like rows that do not exist (404), and
nothing in B may change. The last tests fail if a route is added without a case here.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.db import models
from tests.api.conftest import Caller, SignIn, add_dataset, sign_in, token_caller
from tests.ingest.sample_traces import support_trace


@dataclass
class World:
    a_person: Caller
    a_token: Caller
    b_person: Caller
    b_token: Caller
    b_token_id: str
    b_credential_id: str
    b_price_id: str
    b_dataset_id: str
    b_version_id: str
    b_experiment_id: str
    b_agent_id: str
    b_run_id: str
    b_review_item_id: str
    b_comparison_id: str
    b_batch_id: str
    b_report_id: str
    b_agent_version_id: str


PRICE = {
    "provider": "gemini",
    "model": "m",
    "input_per_million_micros": 1,
    "cached_input_per_million_micros": 1,
    "output_per_million_micros": 1,
    "other_per_million_micros": 1,
}


@pytest.fixture
async def world(new_owner: SignIn, app: FastAPI, clients: Callable[[], httpx.AsyncClient]) -> World:
    a_person, b_person = await new_owner(), await new_owner()
    await add_dataset(app, a_person.workspace_id, "a-data")
    b_dataset_id, b_version_id = await add_dataset(app, b_person.workspace_id, "b-data")
    a_token = await token_caller(a_person, clients(), role="owner")
    b_token = await token_caller(b_person, clients(), role="owner")
    b_token_id = (await b_person.get("/api/v1/api-tokens")).json()[0]["id"]
    credential = await b_person.post(
        "/api/v1/provider-credentials",
        json={"provider": "gemini", "label": "b", "api_key": "AIzaB-secret-key-0000"},
    )
    price = await b_person.post("/api/v1/price-versions", json=PRICE)
    experiment = await b_person.post(
        "/api/v1/experiments",
        json={"agent": "b-bot", "config": {}, "dataset_version_id": b_version_id},
    )
    await b_person.post("/api/v1/traces", json={"traces": [support_trace("b-trace")]})
    async with app.state.sessionmaker() as session:
        b_agent_id = await session.scalar(
            select(models.Agent.id).where(
                models.Agent.workspace_id == b_person.workspace_id,
                models.Agent.name == "support-bot",
            )
        )
        b_run_id = await session.scalar(
            select(models.Run.id).where(models.Run.workspace_id == b_person.workspace_id)
        )
        b_agent_version_id = await session.scalar(
            select(models.AgentVersion.id).where(
                models.AgentVersion.workspace_id == b_person.workspace_id,
                models.AgentVersion.agent_id == b_agent_id,
            )
        )
    await b_person.post("/api/v1/review-items", json={"run_id": str(b_run_id)})
    [b_review] = (await b_person.get("/api/v1/review-queue")).json()
    await b_person.post(
        "/api/v1/provider-credentials",
        json={
            "provider": "gemini",
            "label": "paid",
            "api_key": "AIzaB-paid-key-0000",
            "paid": True,
        },
    )
    await b_person.put(
        f"/api/v1/agents/{b_agent_id}/eval-spec",
        json={"scorers": [{"key": "j", "check": "checklist", "params": {"pack": "support"}}]},
    )
    b_batch = await b_person.post(
        "/api/v1/judge-batches",
        json={
            "agent_id": str(b_agent_id),
            "start": "2026-01-01T00:00:00Z",
            "end": "2027-01-01T00:00:00Z",
        },
    )
    b_comparison = await b_person.post(
        "/api/v1/comparisons",
        json={
            "baseline": {"experiment_id": experiment.json()["id"]},
            "candidate": {"experiment_id": experiment.json()["id"]},
        },
    )
    b_report = await b_person.post(
        "/api/v1/reports", json={"comparison_id": b_comparison.json()["id"]}
    )
    return World(
        a_person,
        a_token,
        b_person,
        b_token,
        b_token_id,
        credential.json()["id"],
        price.json()["id"],
        b_dataset_id,
        b_version_id,
        experiment.json()["id"],
        str(b_agent_id),
        str(b_run_id),
        b_review["id"],
        b_comparison.json()["id"],
        b_batch.json()["id"],
        b_report.json()["id"],
        str(b_agent_version_id),
    )


def _foreign_id_attacks(w: Any) -> list[tuple[str, str, Any]]:
    """(method, path, body) requests from A that name B's rows."""
    return [
        ("POST", f"/api/v1/workspaces/{w.b_person.workspace_id}/invites", {"role": "owner"}),
        ("DELETE", f"/api/v1/api-tokens/{w.b_token_id}", None),
        ("DELETE", f"/api/v1/provider-credentials/{w.b_credential_id}", None),
        ("POST", f"/api/v1/datasets/{w.b_dataset_id}/cases", {"cases": [{"key": "k", "input": 1}]}),
        ("GET", f"/api/v1/dataset-versions/{w.b_version_id}", None),
        ("GET", f"/api/v1/experiments/{w.b_experiment_id}", None),
        ("POST", f"/api/v1/experiments/{w.b_experiment_id}/finish", None),
        ("GET", f"/api/v1/agents/{w.b_agent_id}", None),
        ("GET", f"/api/v1/agents/{w.b_agent_id}/eval-spec", None),
        ("PUT", f"/api/v1/agents/{w.b_agent_id}/eval-spec", {"scorers": []}),
        ("POST", f"/api/v1/agents/{w.b_agent_id}/rescore", {}),
        ("GET", f"/api/v1/runs/{w.b_run_id}", None),
        ("GET", f"/api/v1/agents/{w.b_agent_id}/calibration", None),
        (
            "POST",
            f"/api/v1/review-items/{w.b_review_item_id}/label",
            {"labels": [{"scorer_key": "k", "question_id": "q", "answer": "yes"}]},
        ),
        ("GET", f"/api/v1/comparisons/{w.b_comparison_id}", None),
        ("GET", f"/api/v1/judge-batches/{w.b_batch_id}", None),
        ("GET", f"/api/v1/reports/{w.b_report_id}", None),
        # A body naming B's comparison must not explain it.
        ("POST", "/api/v1/reports", {"comparison_id": w.b_comparison_id}),
        ("GET", f"/api/v1/agents/{w.b_agent_id}/judge-schedule", None),
        ("PUT", f"/api/v1/agents/{w.b_agent_id}/judge-schedule", {"hour": 3}),
        # A body naming B's agent must not judge B's runs.
        (
            "POST",
            "/api/v1/judge-batches",
            {
                "agent_id": w.b_agent_id,
                "start": "2026-01-01T00:00:00Z",
                "end": "2027-01-01T00:00:00Z",
            },
        ),
        (
            "POST",
            "/api/v1/comparisons",
            {
                "baseline": {"judge_batch_id": w.b_batch_id},
                "candidate": {"judge_batch_id": w.b_batch_id},
            },
        ),
        ("GET", f"/api/v1/experiments/{w.b_experiment_id}/baseline", None),
        ("GET", f"/api/v1/agents/{w.b_agent_id}/gates", None),
        ("GET", f"/api/v1/agents/{w.b_agent_id}/health/daily", None),
        ("PUT", f"/api/v1/agents/{w.b_agent_id}/gates", {"max_success_drop": 1}),
        ("POST", f"/api/v1/agents/{w.b_agent_id}/shipped", {"version_id": "x"}),
        ("GET", f"/api/v1/agents/{w.b_agent_id}/experiments", None),
        ("GET", f"/api/v1/agents/{w.b_agent_id}/health", None),
        ("GET", f"/api/v1/agents/{w.b_agent_id}/failures", None),
        ("GET", f"/api/v1/agents/{w.b_agent_id}/alerts", None),
        ("GET", f"/api/v1/review-queue?agent_id={w.b_agent_id}", None),
        ("GET", f"/api/v1/agent-versions/{w.b_agent_version_id}", None),
        ("PATCH", f"/api/v1/agent-versions/{w.b_agent_version_id}", {"label": "stolen"}),
        # Bodies naming B's experiment or agent must not compare B's runs.
        (
            "POST",
            "/api/v1/comparisons",
            {
                "baseline": {"experiment_id": w.b_experiment_id},
                "candidate": {"experiment_id": w.b_experiment_id},
            },
        ),
        (
            "POST",
            "/api/v1/comparisons",
            {
                "baseline": {
                    "agent_id": w.b_agent_id,
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2027-01-01T00:00:00Z",
                },
                "candidate": {
                    "agent_id": w.b_agent_id,
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2027-01-01T00:00:00Z",
                },
            },
        ),
        # A body naming B's run must not put it in A's review queue.
        ("POST", "/api/v1/review-items", {"run_id": w.b_run_id}),
        # A body naming B's dataset version must not start an experiment on it.
        (
            "POST",
            "/api/v1/experiments",
            {"agent": "a", "config": {}, "dataset_version_id": w.b_version_id},
        ),
    ]


async def _send(caller: Caller, method: str, path: str, body: Any) -> httpx.Response:
    if method == "GET":
        return await caller.get(path)
    if method == "DELETE":
        return await caller.delete(path)
    if method == "PUT":
        return await caller.put(path, json=body)
    if method == "PATCH":
        return await caller.patch(path, json=body)
    return await caller.post(path, json=body)


def _b_ids(w: World) -> list[str]:
    return [
        w.b_person.workspace_id,
        w.b_token_id,
        w.b_credential_id,
        w.b_price_id,
        w.b_dataset_id,
        w.b_version_id,
        w.b_experiment_id,
        w.b_agent_id,
        w.b_run_id,
        w.b_review_item_id,
        w.b_comparison_id,
        w.b_batch_id,
        w.b_report_id,
        w.b_agent_version_id,
        str(w.b_person.email),
    ]


async def _b_view(w: World) -> dict[str, Any]:
    view = {
        p: (await w.b_person.get(p)).json()
        for p in ("/api/v1/datasets", "/api/v1/members", "/api/v1/price-versions")
    }
    tokens = (await w.b_person.get("/api/v1/api-tokens")).json()
    view["tokens"] = [{k: t[k] for k in ("id", "revoked")} for t in tokens]
    view["keys"] = (await w.b_person.get("/api/v1/provider-credentials")).json()
    view["experiment"] = (await w.b_person.get(f"/api/v1/experiments/{w.b_experiment_id}")).json()
    view["spec"] = (await w.b_person.get(f"/api/v1/agents/{w.b_agent_id}/eval-spec")).json()
    view["agents"] = (await w.b_person.get("/api/v1/agents")).json()
    view["version"] = (
        await w.b_person.get(f"/api/v1/agent-versions/{w.b_agent_version_id}")
    ).json()
    view["review"] = (await w.b_person.get("/api/v1/review-queue")).json()
    view["gates"] = (await w.b_person.get(f"/api/v1/agents/{w.b_agent_id}/gates")).json()
    view["batch"] = (await w.b_person.get(f"/api/v1/judge-batches/{w.b_batch_id}")).json()
    view["schedule"] = (
        await w.b_person.get(f"/api/v1/agents/{w.b_agent_id}/judge-schedule")
    ).json()
    return view


@pytest.mark.parametrize("who", ["a_person", "a_token"])
async def test_every_foreign_id_looks_missing_and_changes_nothing(world: World, who: str) -> None:
    attacker: Caller = getattr(world, who)
    before = await _b_view(world)
    for method, path, body in _foreign_id_attacks(world):
        response = await _send(attacker, method, path, body)
        assert response.status_code == 404, (method, path, response.status_code, response.text)
        assert response.json() == {"error": "not_found"}, (method, path)
    assert await _b_view(world) == before
    assert (await world.b_token.get("/api/v1/workspaces")).status_code == 200


@pytest.mark.parametrize("who", ["a_person", "a_token"])
async def test_lists_never_show_another_workspace(world: World, who: str) -> None:
    attacker: Caller = getattr(world, who)
    for path in LIST_ROUTES:
        response = await attacker.get(path)
        assert response.status_code == 200, path
        for foreign in _b_ids(world):
            assert foreign not in response.text, (path, foreign)


async def test_workspace_ids_in_bodies_are_ignored(world: World) -> None:
    before = await _b_view(world)
    sneaky = {"workspace_id": world.b_person.workspace_id}
    responses = [
        await world.a_person.post(
            "/api/v1/api-tokens", json={"name": "x", "role": "viewer", **sneaky}
        ),
        await world.a_person.post(
            "/api/v1/provider-credentials",
            json={"provider": "gemini", "label": "x", "api_key": "AIzaA-other-key-0000", **sneaky},
        ),
        await world.a_person.post("/api/v1/price-versions", json={**PRICE, **sneaky}),
        # The same name as B's dataset makes A's own dataset.
        await world.a_person.post(
            "/api/v1/datasets", json={"name": "b-data", "cases": [{"key": "k", "input": 1}]}
        ),
    ]
    assert [r.status_code for r in responses] == [201, 201, 201, 201]
    assert await _b_view(world) == before


async def test_rescoring_cannot_name_another_workspaces_experiment(world: World) -> None:
    await world.a_token.post("/api/v1/traces", json={"traces": [support_trace("a-trace")]})
    [agent] = (await world.a_token.get("/api/v1/agents")).json()
    response = await world.a_token.post(
        f"/api/v1/agents/{agent['id']}/rescore", json={"experiment_id": world.b_experiment_id}
    )
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})


async def test_comparisons_cannot_be_found_by_another_workspaces_experiment(
    world: World,
) -> None:
    path = f"/api/v1/comparisons?candidate_experiment_id={world.b_experiment_id}"
    assert (await world.a_token.get(path)).json() == []
    assert len((await world.b_person.get(path)).json()) == 1


async def test_cases_cannot_be_made_from_another_workspaces_run(world: World) -> None:
    saved = await world.a_person.post(
        "/api/v1/datasets", json={"name": "a-data", "cases": [{"key": "k", "input": 1}]}
    )
    response = await world.a_person.post(
        f"/api/v1/datasets/{saved.json()['dataset_id']}/cases", json={"run_ids": [world.b_run_id]}
    )
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})


async def test_cannot_sign_in_to_a_workspace_you_do_not_belong_to(
    world: World, clients: Callable[[], httpx.AsyncClient]
) -> None:
    client = clients()
    assert world.a_person.email is not None
    with pytest.raises(AssertionError):
        await sign_in(client, world.a_person.email, world.b_person.workspace_id)
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": world.a_person.email,
            "password": "a long enough password",
            "workspace_id": world.b_person.workspace_id,
        },
    )
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})


async def test_traces_belong_only_to_the_workspace_that_sent_them(
    world: World, app: FastAPI
) -> None:
    b_sent = await world.b_person.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    assert b_sent.json()["accepted"] == ["t-1"]
    # A sends a trace with the same id: it is A's own trace, not a duplicate of B's.
    a_sent = await world.a_token.post(
        "/api/v1/traces", json={"traces": [support_trace("t-1", agent="a-bot")]}
    )
    assert a_sent.json()["accepted"] == ["t-1"]
    sneaky = await world.a_token.post(
        "/api/v1/traces",
        json={"traces": [support_trace("t-2", workspace_id=world.b_person.workspace_id)]},
    )
    assert sneaky.json()["accepted"] == [] and len(sneaky.json()["rejected"]) == 1
    # A cannot add a run to B's experiment by tagging a trace with its id.
    tagged = support_trace(
        "t-3", tags={"experiment": world.b_experiment_id, "case": "k", "repetition": 1}
    )
    claimed = await world.a_token.post("/api/v1/traces", json={"traces": [tagged]})
    assert claimed.json()["rejected"][0]["error"].startswith("no experiment")
    async with app.state.sessionmaker() as session:
        b_traces = list(
            await session.scalars(
                select(models.Trace).where(models.Trace.workspace_id == world.b_person.workspace_id)
            )
        )
    assert sorted((t.trace_id, t.agent_name) for t in b_traces) == [
        ("b-trace", "support-bot"),
        ("t-1", "support-bot"),
    ]


async def test_feedback_ids_are_per_workspace(world: World, app: FastAPI) -> None:
    body = {"feedback_id": "fb-1", "trace_id": "t-1", "score": 1}
    b_sent = await world.b_person.post("/api/v1/feedback", json=body)
    # The same feedback id from A is A's own feedback, not a duplicate of B's.
    a_sent = await world.a_token.post(
        "/api/v1/feedback",
        json={**body, "score": 0, "workspace_id": world.b_person.workspace_id},
    )
    assert (b_sent.status_code, a_sent.status_code) == (201, 201)
    async with app.state.sessionmaker() as session:
        b_feedback = list(
            await session.scalars(
                select(models.Feedback).where(
                    models.Feedback.workspace_id == world.b_person.workspace_id
                )
            )
        )
    assert [(f.feedback_id, f.score) for f in b_feedback] == [("fb-1", 1)]


# Every /api/v1 route must be listed in exactly one of these groups.
FOREIGN_ID_ROUTES = {
    ("POST", "/api/v1/workspaces/{workspace_id}/invites"),
    ("DELETE", "/api/v1/api-tokens/{token_id}"),
    ("DELETE", "/api/v1/provider-credentials/{credential_id}"),
    ("POST", "/api/v1/datasets/{dataset_id}/cases"),
    ("GET", "/api/v1/dataset-versions/{version_id}"),
    ("GET", "/api/v1/experiments/{experiment_id}"),
    ("POST", "/api/v1/experiments/{experiment_id}/finish"),
    ("GET", "/api/v1/agents/{agent_id}"),
    ("GET", "/api/v1/agents/{agent_id}/eval-spec"),
    ("PUT", "/api/v1/agents/{agent_id}/eval-spec"),
    ("POST", "/api/v1/agents/{agent_id}/rescore"),
    ("GET", "/api/v1/runs/{run_id}"),
    ("GET", "/api/v1/agents/{agent_id}/calibration"),
    ("POST", "/api/v1/review-items/{item_id}/label"),
    ("GET", "/api/v1/comparisons/{comparison_id}"),
    ("GET", "/api/v1/judge-batches/{batch_id}"),
    ("GET", "/api/v1/reports/{report_id}"),
    ("GET", "/api/v1/agents/{agent_id}/judge-schedule"),
    ("PUT", "/api/v1/agents/{agent_id}/judge-schedule"),
    ("GET", "/api/v1/experiments/{experiment_id}/baseline"),
    ("GET", "/api/v1/agents/{agent_id}/gates"),
    ("GET", "/api/v1/agents/{agent_id}/health/daily"),
    ("PUT", "/api/v1/agents/{agent_id}/gates"),
    ("POST", "/api/v1/agents/{agent_id}/shipped"),
    ("GET", "/api/v1/agents/{agent_id}/experiments"),
    ("GET", "/api/v1/agents/{agent_id}/health"),
    ("GET", "/api/v1/agents/{agent_id}/failures"),
    ("GET", "/api/v1/agents/{agent_id}/alerts"),
    ("GET", "/api/v1/agent-versions/{version_id}"),
    ("PATCH", "/api/v1/agent-versions/{version_id}"),
}
LIST_ROUTES = [
    "/api/v1/agents",
    "/api/v1/review-queue",
    "/api/v1/workspaces",
    "/api/v1/members",
    "/api/v1/datasets",
    "/api/v1/api-tokens",
    "/api/v1/provider-credentials",
    "/api/v1/price-versions",
    "/api/v1/reports",
    "/api/v1/budgets",
    "/api/v1/comparisons",
]
BODY_ROUTES = {
    ("POST", "/api/v1/traces"),
    ("POST", "/api/v1/feedback"),
    ("POST", "/api/v1/review-items"),
    ("POST", "/api/v1/comparisons"),
    ("POST", "/api/v1/judge-batches"),
    ("POST", "/api/v1/reports"),
    ("POST", "/api/v1/datasets"),
    ("POST", "/api/v1/experiments"),
    ("POST", "/api/v1/api-tokens"),
    ("POST", "/api/v1/provider-credentials"),
    ("POST", "/api/v1/price-versions"),
}
# These take no workspace input at all: the workspace comes from the caller or is new.
NO_WORKSPACE_INPUT = {
    ("POST", "/api/v1/auth/setup"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/demo"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/workspaces"),
    ("POST", "/api/v1/invites/accept"),
}


def test_every_route_has_an_isolation_case(app: FastAPI) -> None:
    routes = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/v1")
        for method in operations
    }
    covered = (
        FOREIGN_ID_ROUTES | {("GET", p) for p in LIST_ROUTES} | BODY_ROUTES | NO_WORKSPACE_INPUT
    )
    assert routes - covered == set(), "add an isolation case for these routes"
    assert covered - routes == set(), "these cases name routes that no longer exist"


def test_every_foreign_id_route_is_really_attacked() -> None:
    # Listing a route as covered is not enough: an attack must actually send it.
    fake = SimpleNamespace(
        b_person=SimpleNamespace(workspace_id="x"),
        b_token_id="x",
        b_credential_id="x",
        b_dataset_id="x",
        b_version_id="x",
        b_experiment_id="x",
        b_agent_id="x",
        b_run_id="x",
        b_review_item_id="x",
        b_comparison_id="x",
        b_batch_id="x",
        b_report_id="x",
        b_agent_version_id="x",
    )
    sent = {(method, path) for method, path, _ in _foreign_id_attacks(fake)}
    templates = {(method, re.sub(r"\{[a-z_]+\}", "x", path)) for method, path in FOREIGN_ID_ROUTES}
    assert templates - sent == set(), "these foreign-id routes have no attack"
