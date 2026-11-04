from tests.api.conftest import SCRIPT, SignIn


async def test_scripted_config_is_stored_and_listed(new_owner: SignIn) -> None:
    owner = await new_owner()
    created = await owner.post(
        "/api/v1/agent-configs",
        json={"name": "s", "adapter_type": "scripted", "settings": SCRIPT},
    )
    assert created.status_code == 201
    assert [c["id"] for c in (await owner.get("/api/v1/agent-configs")).json()] == [
        created.json()["id"]
    ]


async def test_bad_settings_are_rejected(new_owner: SignIn) -> None:
    owner = await new_owner()
    bad = await owner.post(
        "/api/v1/agent-configs",
        json={"name": "s", "adapter_type": "scripted", "settings": {"script": {"steps": "no"}}},
    )
    no_url = await owner.post(
        "/api/v1/agent-configs", json={"name": "h", "adapter_type": "http", "settings": {}}
    )
    not_http = await owner.post(
        "/api/v1/agent-configs",
        json={"name": "h", "adapter_type": "http", "settings": {"url": "ftp://agent.example"}},
    )
    good = await owner.post(
        "/api/v1/agent-configs",
        json={"name": "h", "adapter_type": "http", "settings": {"url": "https://agent.example"}},
    )
    assert (bad.status_code, bad.json()["detail"]["error"]) == (422, "settings_invalid")
    assert [no_url.status_code, not_http.status_code, good.status_code] == [422, 422, 201]
