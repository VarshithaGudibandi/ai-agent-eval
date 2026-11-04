import re
from collections.abc import Callable

import httpx
from fastapi import FastAPI

from tests.api.conftest import Caller, SignIn, token_caller
from tests.conftest import GOOD_SCRIPT, UNAPPROVED_SCRIPT, DemoExperiment, RunDemo

HOSTILE_SCRIPT = {**UNAPPROVED_SCRIPT, "final_response": "<script>alert('x')</script> Done."}


async def _compare(owner: Caller, demo: DemoExperiment) -> str:
    baseline, candidate = (
        {"experiment_id": demo.experiment_id, "agent_config_id": c} for c in demo.config_ids
    )
    made = await owner.post(
        "/api/v1/comparisons", json={"baseline": baseline, "candidate": candidate}
    )
    return str(made.json()["id"])


async def test_pages_need_a_signed_in_person(clients: Callable[[], httpx.AsyncClient]) -> None:
    client = clients()
    for path in ("/experiments", "/settings", "/comparisons", "/runs/run_x"):
        response = await client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == f"/login?next={path}"
    login = await client.get("/login")
    assert login.status_code == 200
    assert "script-src 'self'" in login.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in login.headers["content-security-policy"]


async def test_the_click_path_from_experiment_to_the_failing_call(
    new_owner: SignIn, app: FastAPI, run_demo: RunDemo
) -> None:
    owner = await new_owner()
    demo = await run_demo(
        app.state.sessionmaker, owner.workspace_id, [GOOD_SCRIPT, UNAPPROVED_SCRIPT]
    )
    listing = await owner.get("/experiments")
    assert demo.experiment_id in listing.text

    page = await owner.get(f"/experiments/{demo.experiment_id}")
    assert page.status_code == 200
    assert "Scorecard" in page.text and "Demo experiment" in page.text
    assert "UNAPPROVED_REFUND" in page.text

    failures = await owner.get(
        f"/experiments/{demo.experiment_id}/runs", params={"reason_code": "UNAPPROVED_REFUND"}
    )
    [run_id] = re.findall(r'href="/runs/(run_[0-9a-f]+)"', failures.text)

    run = await owner.get(f"/runs/{run_id}")
    assert run.status_code == 200
    assert "Hidden ground truth" in run.text
    # Each failing finding links to an event that is on the same page.
    targets = set(re.findall(r'href="#(evt_[0-9a-f]+)"', run.text))
    assert targets
    assert targets <= set(re.findall(r'<li id="(evt_[0-9a-f]+)"', run.text))


async def test_agent_text_is_shown_as_text_never_as_markup(
    new_owner: SignIn, app: FastAPI, run_demo: RunDemo
) -> None:
    owner = await new_owner()
    demo = await run_demo(app.state.sessionmaker, owner.workspace_id, [HOSTILE_SCRIPT])
    runs = await owner.get(f"/experiments/{demo.experiment_id}/runs")
    [run_id] = re.findall(r'href="/runs/(run_[0-9a-f]+)"', runs.text)
    page = await owner.get(f"/runs/{run_id}")
    assert "<script>alert" not in page.text
    assert "&lt;script&gt;alert" in page.text


async def test_comparison_pages_put_regressions_first(
    new_owner: SignIn, app: FastAPI, run_demo: RunDemo
) -> None:
    owner = await new_owner()
    demo = await run_demo(
        app.state.sessionmaker, owner.workspace_id, [GOOD_SCRIPT, UNAPPROVED_SCRIPT]
    )
    comparison_id = await _compare(owner, demo)
    listing = await owner.get("/comparisons")
    assert comparison_id in listing.text
    page = await owner.get(f"/comparisons/{comparison_id}")
    assert page.status_code == 200
    assert "regressed" in page.text
    assert "may be noise" in page.text


async def test_other_workspaces_pages_look_missing(
    new_owner: SignIn, app: FastAPI, run_demo: RunDemo
) -> None:
    owner, stranger = await new_owner(), await new_owner()
    demo = await run_demo(
        app.state.sessionmaker, owner.workspace_id, [GOOD_SCRIPT, UNAPPROVED_SCRIPT]
    )
    comparison_id = await _compare(owner, demo)
    runs = await owner.get(f"/experiments/{demo.experiment_id}/runs")
    run_id = re.findall(r'href="/runs/(run_[0-9a-f]+)"', runs.text)[0]
    for path in (
        f"/experiments/{demo.experiment_id}",
        f"/experiments/{demo.experiment_id}/runs",
        f"/runs/{run_id}",
        f"/comparisons/{comparison_id}",
    ):
        response = await stranger.get(path)
        assert response.status_code == 404, path
        assert "Not found" in response.text
        assert demo.experiment_id not in response.text
    assert demo.experiment_id not in (await stranger.get("/experiments")).text
    assert comparison_id not in (await stranger.get("/comparisons")).text


async def test_settings_show_owner_tools_only_to_owners(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    page = await owner.get("/settings")
    assert page.status_code == 200
    assert "API tokens" in page.text and "Provider keys" in page.text
    assert str(owner.email) in page.text
    invite = await owner.post(
        f"/api/v1/workspaces/{owner.workspace_id}/invites", json={"role": "viewer"}
    )
    viewer_client = clients()
    accepted = await viewer_client.post(
        "/api/v1/invites/accept",
        json={
            "token": invite.json()["token"],
            "email": "viewer@example.com",
            "password": "a long enough password",
        },
    )
    assert accepted.status_code == 200
    await viewer_client.post(
        "/api/v1/auth/login",
        json={
            "email": "viewer@example.com",
            "password": "a long enough password",
            "workspace_id": owner.workspace_id,
        },
    )
    seen = await viewer_client.get("/settings")
    assert seen.status_code == 200
    assert "API tokens" not in seen.text and "Provider keys" not in seen.text


async def test_api_tokens_cannot_open_pages(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    bot = await token_caller(owner, clients(), role="owner")
    assert (await bot.get("/experiments")).status_code == 303


async def test_static_files_are_served(clients: Callable[[], httpx.AsyncClient]) -> None:
    client = clients()
    for path in ("/static/app.js", "/static/style.css", "/static/htmx.min.js"):
        assert (await client.get(path)).status_code == 200, path
