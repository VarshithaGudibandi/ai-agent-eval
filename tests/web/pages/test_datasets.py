from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI

from tests.api.conftest import SignIn, viewer_caller
from tests.api.seeding import add_dataset, run_experiment, send_live
from tests.ingest.sample_traces import CONFIG

CASES = [
    {
        "key": "refund-1",
        "input": {"message": "broken"},
        "expected": {"refund": 59.0},
        "tags": {"category": "refund"},
    },
    {
        "key": "lookup-1",
        "input": {"message": "<script>alert(1)</script>"},
        "tags": {"category": "lookup"},
    },
]


async def test_no_datasets_shows_the_file_format(new_owner: SignIn) -> None:
    owner = await new_owner()
    page = (await owner.get("/datasets")).text
    assert "No datasets yet" in page and "JSON Lines" in page


async def test_a_dataset_version_shows_its_cases_and_their_last_result(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    first = await add_dataset(owner, CASES)
    listed = (await owner.get("/datasets")).text
    assert f'href="/dataset-versions/{first}"' in listed
    never = (await owner.get(f"/dataset-versions/{first}")).text
    assert "never run" in never and "Have run</dt><dd>0 of 2" in never
    await run_experiment(owner, app, first, CONFIG, crash={1}, cases=CASES)
    page = (await owner.get(f"/dataset-versions/{first}")).text
    assert "Pass" in page and "Fail" in page and "Have run</dt><dd>2 of 2" in page
    assert "category=refund (1)" in page and "category=lookup (1)" in page
    assert "&lt;script&gt;" in page and "<script>alert" not in page
    assert 'data-type="jsonl"' in page and 'data-type="list"' in page


async def test_an_older_version_points_to_the_newest(new_owner: SignIn) -> None:
    owner = await new_owner()
    first = await add_dataset(owner, CASES)
    [dataset] = (await owner.get("/api/v1/datasets")).json()
    newer = await owner.post(
        f"/api/v1/datasets/{dataset['id']}/cases", json={"cases": [{"key": "new-1", "input": 1}]}
    )
    page = (await owner.get(f"/dataset-versions/{first}")).text
    assert (
        "v1 of 2" in page
        and f'href="/dataset-versions/{newer.json()["id"]}">see the newest' in page
    )


async def test_viewers_cannot_add_cases(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    first = await add_dataset(owner, CASES)
    viewer = await viewer_caller(owner, clients())
    page = (await viewer.get(f"/dataset-versions/{first}")).text
    assert "A member can add cases." in page and 'data-type="jsonl"' not in page


async def test_failing_live_runs_can_become_cases_from_the_agent_page(
    new_owner: SignIn, app: FastAPI
) -> None:
    owner = await new_owner()
    await add_dataset(owner, CASES)
    await send_live(owner, app, "live", datetime.now(UTC) - timedelta(days=1), passed=2, failed=3)
    [agent] = (await owner.get("/api/v1/agents")).json()
    page = (await owner.get(f"/agents/{agent['id']}")).text
    assert 'data-api="/api/v1/datasets/{dataset_id}/cases"' in page
    assert "Turn these 3 failing runs into test cases" in page


async def test_another_workspaces_dataset_is_not_found(new_owner: SignIn) -> None:
    owner, other = await new_owner(), await new_owner()
    theirs = await add_dataset(other, CASES)
    page = await owner.get(f"/dataset-versions/{theirs}")
    assert page.status_code == 404 and "refund-1" not in page.text
