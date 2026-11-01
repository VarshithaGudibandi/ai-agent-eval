from app.auth.passwords import hash_password, verify_password


def test_hash_is_argon2id_and_verifies() -> None:
    hashed = hash_password("correct horse battery")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "correct horse battery")
    assert not verify_password(hashed, "wrong horse battery")


def test_unknown_user_never_verifies() -> None:
    assert not verify_password(None, "not-a-real-password")
    assert not verify_password("garbage", "anything")
