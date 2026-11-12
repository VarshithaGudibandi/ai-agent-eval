from fastapi import FastAPI

from tests.api.conftest import SignIn, add_dataset


async def test_datasets_list_with_their_versions(new_owner: SignIn, app: FastAPI) -> None:
    owner = await new_owner()
    assert (await owner.get("/api/v1/datasets")).json() == []
    dataset_id, version_id = await add_dataset(app, owner.workspace_id, "support")
    [dataset] = (await owner.get("/api/v1/datasets")).json()
    assert (dataset["id"], dataset["name"]) == (dataset_id, "support")
    assert dataset["versions"] == [{"id": version_id, "version": 1, "content_hash": "h"}]
