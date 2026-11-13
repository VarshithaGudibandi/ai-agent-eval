from datetime import datetime
from typing import Any

from jinja2 import Environment

# How numbers read on every page. A missing value is a dash, never a zero: unknown is not
# the same as none.

DASH = "\u2013"


def when(value: datetime | None) -> str:
    return "" if value is None else value.strftime("%Y-%m-%d %H:%M UTC")


def day(value: datetime | None) -> str:
    return "" if value is None else value.strftime("%b %d")


def percent(value: float | None) -> str:
    return DASH if value is None else f"{value * 100:.0f}%"


def points(value: float | None) -> str:
    """A change in a share, in percentage points."""
    return DASH if value is None else f"{value * 100:+.1f} pts"


def duration(ms: float | None) -> str:
    if ms is None:
        return DASH
    return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


def money(micros: float | None) -> str:
    if micros is None:
        return DASH
    return f"${micros / 1_000_000:.4f}" if micros < 10_000_000 else f"${micros / 1_000_000:.2f}"


def count(value: int | None) -> str:
    return DASH if value is None else f"{value:,}"


def short(value: str | None, length: int = 12) -> str:
    """An id or fingerprint cut to what a person can compare at a glance."""
    if not value:
        return ""
    return value if len(value) <= length else value[:length] + "…"


def version_name(version: Any) -> str:
    """A version's label if it has one, else the start of its fingerprint."""
    return str(version.label) if version.label else short(version.fingerprint, 8)


def register(env: Environment) -> None:
    env.filters.update(
        when=when,
        day=day,
        percent=percent,
        points=points,
        duration=duration,
        money=money,
        count=count,
        short=short,
        version_name=version_name,
    )
