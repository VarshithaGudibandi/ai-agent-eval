import re
from typing import Any

# Secrets are masked before a trace is stored and in every log line. A trace is the agent's
# own record of what it saw, so it can carry anything its tools returned, keys included.

MASK = "[redacted]"

_SECRET = re.compile(
    "|".join(
        [
            # This platform's own tokens.
            r"\b(?:aet|ses|csrf|inv)_[A-Za-z0-9_-]{20,}",
            # Provider keys with known shapes: Google, OpenAI and Anthropic style, AWS, GitHub,
            # Slack.
            r"\bAIza[0-9A-Za-z_-]{20,}",
            r"\bsk-[A-Za-z0-9_-]{20,}",
            r"\bAKIA[0-9A-Z]{16}\b",
            r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{30,}",
            r"\bgithub_pat_[A-Za-z0-9_]{30,}",
            r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
            # Signed tokens: three base64url parts, the first starting like a JSON header.
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            r"(?i:bearer\s+)[A-Za-z0-9._~+/=-]{8,}",
        ]
    ),
    re.DOTALL,
)

# A text value under a name ending like these is a secret whatever it looks like, such as a
# password. The word must end the name, so "time_to_first_token_ms" is left alone.
_SECRET_NAME = re.compile(
    r"(?i)(?:^|[_-])(?:password|passwd|secret|api[_-]?key|token|authorization|cookie)$"
)


def redact(text: str) -> str:
    return _SECRET.sub(MASK, text)


def redact_value(value: Any) -> Any:
    """A copy of JSON-like data with every secret in it masked."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, dict):
        return {
            key: MASK
            if isinstance(key, str) and _SECRET_NAME.search(key) and _is_scalar_secret(item)
            else redact_value(item)
            for key, item in value.items()
        }
    return value


def _is_scalar_secret(value: Any) -> bool:
    # Only text can be a secret. Counts and timings under such names are left readable, an
    # empty value hides nothing, and a nested object is searched instead.
    return isinstance(value, str) and value != ""
