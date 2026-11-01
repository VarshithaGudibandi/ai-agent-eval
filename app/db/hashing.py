import hashlib
import json
from typing import Any


def content_hash(value: Any) -> str:
    # Sorted keys and fixed separators make the hash independent of dict order and whitespace.
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()
