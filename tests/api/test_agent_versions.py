from collections.abc import Callable

import httpx

from tests.api.conftest import SignIn, token_caller
from tests.ingest.sample_traces import CONFIG, support_trace

V2 = {**CONFIG, "prompt": CONFIG["prompt"] + " Always be brief.", "temperature": 0.9}


async def test_a_version_shows_its_config_and_what_changed_since_the_one_before(
    new_owner: SignIn,
) -> None:
    owner = await new_owner()
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-2", config=V2)]})
    [agent] = (await owner.get("/api/v1/agents")).json()
    first_id, second_id = (v["id"] for v in agent["versions"])
    first = (await owner.get(f"/api/v1/agent-versions/{first_id}")).json()
    assert (first["previous_version_id"], first["changes"]) == (None, [])
    assert first["config"]["prompt"] == CONFIG["prompt"]
    second = (await owner.get(f"/api/v1/agent-versions/{second_id}")).json()
    assert second["previous_version_id"] == first_id
    assert {c["field"] for c in second["changes"]} == {"prompt", "settings"}
    [prompt] = [c for c in second["changes"] if c["field"] == "prompt"]
    assert prompt["detail"] == ["+Always be brief."]


async def test_members_can_label_a_version_and_viewers_cannot(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    await owner.post("/api/v1/traces", json={"traces": [support_trace("t-1")]})
    [agent] = (await owner.get("/api/v1/agents")).json()
    path = f"/api/v1/agent-versions/{agent['versions'][0]['id']}"
    viewer = await token_caller(owner, clients(), role="viewer")
    assert (await viewer.patch(path, {"label": "v1"})).status_code == 403
    labelled = await owner.patch(path, {"label": "v1"})
    assert (labelled.status_code, labelled.json()["label"]) == (200, "v1")
    [agent] = (await owner.get("/api/v1/agents")).json()
    assert agent["versions"][0]["label"] == "v1"
    cleared = await owner.patch(path, {"label": None})
    assert cleared.json()["label"] is None
    refused = await owner.patch(path, {"label": ""})
    assert refused.status_code == 422
