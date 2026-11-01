import base64
import binascii
import os
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.settings import Settings


class MasterKeyMissingError(RuntimeError):
    pass


class KeyUnreadableError(RuntimeError):
    pass


class KeyVault(Protocol):
    """Encrypts provider keys. A hosted install swaps in a cloud key manager here."""

    def encrypt(self, plaintext: str, context: str) -> tuple[bytes, bytes]:
        """Return (nonce, ciphertext). The context must be given again to decrypt."""
        ...

    def decrypt(self, nonce: bytes, ciphertext: bytes, context: str) -> str:
        """Return the plaintext, or raise KeyUnreadableError."""
        ...


class LocalKeyVault:
    """AES-GCM with a master key from the environment."""

    def __init__(self, master_key: bytes) -> None:
        self._aead = AESGCM(master_key)

    def encrypt(self, plaintext: str, context: str) -> tuple[bytes, bytes]:
        nonce = os.urandom(12)
        return nonce, self._aead.encrypt(nonce, plaintext.encode(), context.encode())

    def decrypt(self, nonce: bytes, ciphertext: bytes, context: str) -> str:
        try:
            return self._aead.decrypt(nonce, ciphertext, context.encode()).decode()
        except InvalidTag:
            raise KeyUnreadableError("key cannot be decrypted with this master key") from None


def key_vault_from_settings(settings: Settings) -> LocalKeyVault:
    if settings.app_master_key is None:
        raise MasterKeyMissingError("APP_MASTER_KEY is not set")
    try:
        key = base64.b64decode(settings.app_master_key.get_secret_value(), validate=True)
    except (binascii.Error, ValueError):
        raise MasterKeyMissingError("APP_MASTER_KEY is not valid base64") from None
    if len(key) != 32:
        raise MasterKeyMissingError("APP_MASTER_KEY must decode to 32 bytes")
    return LocalKeyVault(key)
