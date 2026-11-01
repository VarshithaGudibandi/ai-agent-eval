import re
from collections import defaultdict
from difflib import SequenceMatcher
from itertools import combinations
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from app.db.hashing import content_hash
from app.environments.contract import AgentTaskInput

# Ids such as ord_1001, cust_12 or tkt_7. Renaming them must not hide a copied task.
_ID = re.compile(r"\b[a-z]{2,8}_[a-z0-9]+\b", re.IGNORECASE)
# Text this close after normalizing reads as the same task reworded.
SIMILAR_TEXT = 0.85


class Splittable(Protocol):
    @property
    def task_key(self) -> str: ...
    @property
    def scenario_family(self) -> str: ...
    @property
    def split(self) -> str: ...
    @property
    def agent_input(self) -> AgentTaskInput: ...
    @property
    def fixture(self) -> dict[str, Any]: ...


class Leak(BaseModel):
    kind: Literal["family_spans_splits", "same_fixture", "similar_text"]
    task_keys: list[str]
    splits: list[str]


def normalized_text(text: str) -> str:
    text = _ID.sub(" id ", text.lower())
    text = re.sub(r"\d+", "0", text)
    return " ".join(re.findall(r"[a-z0]+", text))


def fixture_shape(fixture: dict[str, Any]) -> str:
    """A hash of the fixture with every id replaced by its order of first appearance."""
    names: dict[str, str] = {}

    def rename(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: rename(v) for k, v in value.items()}
        if isinstance(value, list):
            return [rename(v) for v in value]
        if isinstance(value, str) and _ID.fullmatch(value):
            return names.setdefault(value, f"id{len(names)}")
        return value

    return content_hash(rename(fixture))


def _similar(a: str, b: str) -> bool:
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    # The quick ratios are upper bounds, so most pairs are ruled out cheaply.
    return (
        matcher.real_quick_ratio() >= SIMILAR_TEXT
        and matcher.quick_ratio() >= SIMILAR_TEXT
        and matcher.ratio() >= SIMILAR_TEXT
    )


def find_leaks(tasks: list[Splittable]) -> list[Leak]:
    """Ways a task could leak from one split into another.

    Splits are assigned per scenario family, so a family must sit in one split. Across
    splits, two tasks with the same world under renamed ids, or with nearly the same
    request, are the same task in disguise.
    """
    leaks: list[Leak] = []
    families: dict[str, list[Splittable]] = defaultdict(list)
    for task in tasks:
        families[task.scenario_family].append(task)
    for members in families.values():
        splits = sorted({t.split for t in members})
        if len(splits) > 1:
            leaks.append(
                Leak(
                    kind="family_spans_splits",
                    task_keys=sorted(t.task_key for t in members),
                    splits=splits,
                )
            )
    texts = {t.task_key: normalized_text(t.agent_input.user_message) for t in tasks}
    shapes = {t.task_key: fixture_shape(t.fixture) for t in tasks}
    for a, b in combinations(tasks, 2):
        if a.split == b.split:
            continue
        pair = {"task_keys": [a.task_key, b.task_key], "splits": [a.split, b.split]}
        if shapes[a.task_key] == shapes[b.task_key]:
            leaks.append(Leak(kind="same_fixture", **pair))
        elif _similar(texts[a.task_key], texts[b.task_key]):
            leaks.append(Leak(kind="similar_text", **pair))
    return leaks
