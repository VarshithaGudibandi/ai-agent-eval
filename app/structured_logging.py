import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}

# Our own secrets have known prefixes, and Google keys start with AIza. Anything shaped
# like one is masked in every log line, whatever code wrote it.
_SECRET = re.compile(
    r"\b(?:(?:aet|ses|csrf|inv)_[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{20,})"
    r"|(?i:bearer\s+)[A-Za-z0-9._~+/=-]{8,}"
)


def redact(text: str) -> str:
    return _SECRET.sub("[redacted]", text)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update({k: v for k, v in vars(record).items() if k not in _RESERVED})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str))


def configure_logging(level: str, fmt: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
