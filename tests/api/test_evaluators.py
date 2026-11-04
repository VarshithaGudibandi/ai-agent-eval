from tests.api.conftest import SignIn


async def test_evaluators_publish_their_versions_and_what_they_catch(new_owner: SignIn) -> None:
    owner = await new_owner()
    response = await owner.get("/api/v1/evaluators")
    assert response.status_code == 200
    body = response.json()
    by_name = {e["name"]: e for e in body["evaluators"]}
    assert by_name["refund_outcome"]["version"] == "1.1.0"
    wrong_amount = {
        "mutation": "wrong_amount",
        "dimension": "refund_amount",
        "reason_code": "WRONG_REFUND_AMOUNT",
    }
    assert wrong_amount in by_name["refund_outcome"]["catches"]
    assert body["not_covered"]
