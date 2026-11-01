from app.auth.tokens import hash_secret, new_secret, secrets_match


def test_secrets_are_random_prefixed_and_stored_only_as_hashes() -> None:
    a, b = new_secret("aet"), new_secret("aet")
    assert a != b
    assert a.startswith("aet_")
    assert hash_secret(a) != a
    assert secrets_match(a, hash_secret(a))
    assert not secrets_match(b, hash_secret(a))
