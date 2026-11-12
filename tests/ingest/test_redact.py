import pytest

from app.ingest.redact import MASK, redact, redact_value

# Built at runtime so no key-shaped literal ever sits in the repository.
FAKE = {
    "google": "AIza" + "B" * 35,
    "openai style": "sk-" + "proj_" + "c" * 32,
    "aws": "AKIA" + "Q" * 16,
    "github": "ghp_" + "d" * 36,
    "slack": "xoxb-" + "1234567890-abcdefghij",
    "jwt": "eyJ" + "hbGciOiJIUzI1" + "." + "e" * 20 + "." + "f" * 20,
    "ours": "aet_" + "g" * 32,
    "bearer": "Bearer " + "h" * 24,
    "private key": "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
}


@pytest.mark.parametrize("kind", list(FAKE))
def test_known_secret_shapes_are_masked_inside_text(kind: str) -> None:
    text = f"the tool said: {FAKE[kind]} and nothing else"
    masked = redact(text)
    assert FAKE[kind] not in masked
    assert MASK in masked
    assert masked.startswith("the tool said:") and masked.endswith("nothing else")


def test_ordinary_text_is_left_alone() -> None:
    text = "Order ord_1001 shipped on 2026-11-03; refund apr_0001 is pending. Ask-me anything."
    assert redact(text) == text


def test_values_under_secret_names_are_masked_whatever_they_look_like() -> None:
    data = {
        "password": "hunter2",
        "user_api_key": "short",
        "Authorization": "Basic dXNlcjpwYXNz",
        "refresh-token": 12345,
        "session_cookie": "c=1",
        "token_usage": {"input": 10, "output": 5},
        "max_tokens": "",
        "keywords": "secretly not a secret name",
    }
    masked = redact_value(data)
    for key in ("password", "user_api_key", "Authorization", "refresh-token", "session_cookie"):
        assert masked[key] == MASK, key
    assert masked["token_usage"] == {"input": 10, "output": 5}
    assert masked["max_tokens"] == ""
    assert masked["keywords"] == data["keywords"]


def test_nested_data_is_searched_and_not_changed_in_place() -> None:
    key = FAKE["google"]
    data = {"spans": [{"output": {"headers": [f"x-goog-api-key: {key}"]}, "n": 3, "ok": True}]}
    masked = redact_value(data)
    assert masked["spans"][0]["output"]["headers"] == [f"x-goog-api-key: {MASK}"]
    assert (masked["spans"][0]["n"], masked["spans"][0]["ok"]) == (3, True)
    assert key in data["spans"][0]["output"]["headers"][0]
