from datetime import datetime

from jinja2 import Environment


def when(value: datetime | None) -> str:
    return "" if value is None else value.strftime("%Y-%m-%d %H:%M UTC")


def register(env: Environment) -> None:
    env.filters.update(when=when)
