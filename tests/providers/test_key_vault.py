import base64

import pytest

from app.providers.key_vault import (
    KeyUnreadableError,
    LocalKeyVault,
    MasterKeyMissingError,
    key_vault_from_settings,
)
from app.settings import Settings


def test_round_trip_needs_the_same_key_and_context() -> None:
    vault = LocalKeyVault(b"k" * 32)
    nonce, ciphertext = vault.encrypt("AIza-secret", "ws_1:cred_1:gemini")
    assert b"AIza" not in ciphertext
    assert vault.decrypt(nonce, ciphertext, "ws_1:cred_1:gemini") == "AIza-secret"
    with pytest.raises(KeyUnreadableError):
        vault.decrypt(nonce, ciphertext, "ws_2:cred_1:gemini")
    with pytest.raises(KeyUnreadableError):
        LocalKeyVault(b"x" * 32).decrypt(nonce, ciphertext, "ws_1:cred_1:gemini")


def test_each_encryption_uses_a_fresh_nonce() -> None:
    vault = LocalKeyVault(b"k" * 32)
    assert vault.encrypt("same", "c")[0] != vault.encrypt("same", "c")[0]


@pytest.mark.parametrize("value", [None, "not base64!", base64.b64encode(b"short").decode()])
def test_bad_or_missing_master_key_is_refused(value: str | None) -> None:
    with pytest.raises(MasterKeyMissingError):
        key_vault_from_settings(Settings(app_master_key=value))
