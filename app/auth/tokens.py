import hashlib
import hmac
import secrets


def new_secret(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def hash_secret(secret: str) -> str:
    # The secrets are 256 random bits, so a fast hash is enough; no one can guess them.
    return hashlib.sha256(secret.encode()).hexdigest()


def secrets_match(secret: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), expected_hash)
