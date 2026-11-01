from datetime import datetime

from sqlalchemy import func, select

from app.db import models
from app.db.scope import WorkspaceScope


async def take(scope: WorkspaceScope, credential_id: str, tokens: int) -> float:
    """Take one request and some tokens from the key's bucket.

    Returns 0 if they were taken, otherwise how many seconds to wait before trying again.
    The row lock makes every worker share one bucket per key.
    """
    row = await scope.session.scalar(
        scope.query(models.ProviderCredential)
        .where(models.ProviderCredential.id == credential_id)
        .with_for_update()
    )
    if row is None:
        raise LookupError(credential_id)
    rpm, tpm = row.requests_per_minute, row.tokens_per_minute
    if rpm is None and tpm is None:
        return 0.0
    now = await scope.session.scalar(select(func.clock_timestamp()))
    if not isinstance(now, datetime):
        raise RuntimeError("database did not return a timestamp")
    elapsed = (now - row.bucket_refreshed_at).total_seconds() if row.bucket_refreshed_at else None
    requests = _refill(row.bucket_requests, rpm, elapsed)
    available_tokens = _refill(row.bucket_tokens, tpm, elapsed)
    # A call bigger than a whole minute's tokens could never fit, so it waits for a full bucket.
    need_tokens = min(tokens, tpm) if tpm is not None else 0
    wait = max(
        _wait(requests, 1, rpm),
        _wait(available_tokens, need_tokens, tpm),
    )
    if wait == 0:
        requests = None if requests is None else requests - 1
        available_tokens = None if available_tokens is None else available_tokens - need_tokens
    row.bucket_requests = requests
    row.bucket_tokens = available_tokens
    row.bucket_refreshed_at = now
    return wait


def _refill(level: float | None, per_minute: int | None, elapsed: float | None) -> float | None:
    if per_minute is None:
        return None
    if level is None or elapsed is None:
        return float(per_minute)
    return min(float(per_minute), level + elapsed * per_minute / 60)


def _wait(level: float | None, need: float, per_minute: int | None) -> float:
    if level is None or per_minute is None or level >= need:
        return 0.0
    return (need - level) * 60 / per_minute
