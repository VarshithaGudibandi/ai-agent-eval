from app.agents.signing import sign, verify

BODY = b'{"kind": "task"}'


def test_a_signature_verifies_only_for_the_same_secret_and_body() -> None:
    header = sign("whsec_a", BODY, timestamp=1_000)
    assert verify("whsec_a", header, BODY, now=1_000)
    assert not verify("whsec_b", header, BODY, now=1_000)
    assert not verify("whsec_a", header, BODY + b" ", now=1_000)


def test_old_or_malformed_signatures_are_refused() -> None:
    header = sign("whsec_a", BODY, timestamp=1_000)
    assert not verify("whsec_a", header, BODY, now=1_000 + 301)
    assert not verify("whsec_a", "v1=abc", BODY, now=1_000)
    assert not verify("whsec_a", "t=soon,v1=abc", BODY, now=1_000)
