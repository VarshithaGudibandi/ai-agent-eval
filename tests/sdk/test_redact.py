from agent_eval import redact as sdk
from app.ingest import redact as platform


def test_the_sdk_masks_exactly_what_the_platform_masks() -> None:
    # Two copies of one rule set: if they drift, a config could fingerprint differently on
    # each side and a secret the platform knows could leave the agent's process.
    assert sdk._SECRET.pattern == platform._SECRET.pattern
    assert sdk._SECRET.flags == platform._SECRET.flags
    assert sdk._SECRET_NAME.pattern == platform._SECRET_NAME.pattern
    assert sdk.MASK == platform.MASK
    sample = {
        "api_key": "plain",
        "note": "AIza" + "B" * 30,
        "tokens": 3,
        "list": ["sk-" + "c" * 25],
    }
    assert sdk.redact_value(sample) == platform.redact_value(sample)
