from tests.api.conftest import SignIn
from tests.api.seeding import add_dataset
from tests.ingest.sample_traces import CONFIG


async def test_budgets_are_listed_with_their_owner_and_use(new_owner: SignIn) -> None:
    owner = await new_owner()
    assert (await owner.get("/api/v1/budgets")).json() == []
    dataset_id = await add_dataset(owner)
    experiment = (
        await owner.post(
            "/api/v1/experiments",
            json={"agent": "support-bot", "config": CONFIG, "dataset_version_id": dataset_id},
        )
    ).json()
    [budget] = (await owner.get("/api/v1/budgets")).json()
    assert (budget["owner_kind"], budget["owner_id"]) == ("experiment", experiment["id"])
    assert (budget["requests_used"], budget["money_used_micros"]) == (0, 0)
