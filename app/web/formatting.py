import json
from datetime import datetime
from typing import Any

from jinja2 import Environment


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def money(micros: int | None, currency: str = "USD") -> str:
    if micros is None:
        return "n/a"
    return f"{micros / 1_000_000:,.4f} {currency}"


def duration(ms: int | None) -> str:
    if ms is None:
        return "n/a"
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


def pretty(value: Any) -> str:
    # Rendered as escaped text, so tool output and agent replies stay inert.
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str)


def when(value: datetime | None) -> str:
    return "" if value is None else value.strftime("%Y-%m-%d %H:%M UTC")


def bar(numerator: float, denominator: float | None) -> int:
    """A width from 0 to 100 for a progress or budget bar."""
    if not denominator:
        return 0
    return max(0, min(100, round(numerator / denominator * 100)))


def register(env: Environment) -> None:
    env.filters.update(percent=percent, money=money, duration=duration, pretty=pretty, when=when)
    env.globals["bar"] = bar
