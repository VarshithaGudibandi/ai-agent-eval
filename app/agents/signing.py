import hashlib
import hmac
import time

SIGNATURE_HEADER = "x-agent-eval-signature"
# A request older than this is refused, so a captured one cannot be replayed later.
TOLERANCE_SECONDS = 300


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    """The signature header for a request body: "t=<unix time>,v1=<hex HMAC-SHA256>".

    The time is signed with the body, so a receiver can reject old requests.
    """
    stamp = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={digest}"


def verify(secret: str, header: str, body: bytes, now: int | None = None) -> bool:
    """What a receiving agent does with the header. The example agent does the same."""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        stamp = int(parts["t"])
    except (KeyError, ValueError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - stamp) > TOLERANCE_SECONDS:
        return False
    return hmac.compare_digest(sign(secret, body, stamp), header)
