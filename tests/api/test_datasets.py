from tests.api.conftest import TASK, SignIn


async def test_import_creates_versions_and_lists_them(new_owner: SignIn) -> None:
    owner = await new_owner()
    first = await owner.post("/api/v1/datasets/import", json={"name": "retail", "tasks": [TASK]})
    second = await owner.post("/api/v1/datasets/import", json={"name": "retail", "tasks": [TASK]})
    assert [first.json()["version"], second.json()["version"]] == [1, 2]
    [dataset] = (await owner.get("/api/v1/datasets")).json()
    assert [v["task_count"] for v in dataset["versions"]] == [1, 1]


async def test_import_rejects_unknown_environments_and_duplicate_keys(new_owner: SignIn) -> None:
    owner = await new_owner()
    unknown = await owner.post(
        "/api/v1/datasets/import",
        json={"name": "x", "tasks": [{**TASK, "environment": "nowhere@1"}]},
    )
    duplicate = await owner.post(
        "/api/v1/datasets/import", json={"name": "x", "tasks": [TASK, TASK]}
    )
    assert unknown.status_code == duplicate.status_code == 422
    assert (await owner.get("/api/v1/datasets")).json() == []
