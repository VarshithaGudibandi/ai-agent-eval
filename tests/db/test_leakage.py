import copy
from typing import Any

from app.db import models
from app.db.content import TaskDefinition, load_task_file
from app.db.leakage import find_leaks, fixture_shape, normalized_text
from tests.conftest import TASK_FILE

BASE = load_task_file(TASK_FILE)


def _task(
    key: str, family: str, split: str, message: str | None = None, **fixture: Any
) -> TaskDefinition:
    world = copy.deepcopy(BASE.fixture)
    world["orders"][0].update(fixture)
    return BASE.model_copy(
        update={
            "task_key": key,
            "scenario_family": family,
            "split": models.Split(split),
            "agent_input": BASE.agent_input.model_copy(
                update={"user_message": message or BASE.agent_input.user_message}
            ),
            "fixture": world,
        }
    )


def _renamed(task: TaskDefinition, key: str, family: str, split: str) -> TaskDefinition:
    text = TaskDefinition.model_validate(task.model_dump()).model_dump_json()
    for old, new in (("ord_1001", "ord_5150"), ("cust_12", "cust_77"), ("ord_2002", "ord_6060")):
        text = text.replace(old, new)
    copied = TaskDefinition.model_validate_json(text)
    return copied.model_copy(
        update={"task_key": key, "scenario_family": family, "split": models.Split(split)}
    )


def test_ids_and_numbers_do_not_change_normalized_text() -> None:
    assert normalized_text("Refund ORD_1001 for $249.99, please!") == normalized_text(
        "refund ord_7 for $1.00 please"
    )


def test_fixture_shape_ignores_id_names_but_not_amounts() -> None:
    task = _task("a", "f", "dev")
    assert fixture_shape(task.fixture) == fixture_shape(_renamed(task, "b", "g", "dev").fixture)
    assert fixture_shape(task.fixture) != fixture_shape(
        _task("c", "f", "dev", total_minor=1).fixture
    )


def test_distinct_tasks_in_distinct_splits_are_fine() -> None:
    dev = _task("a", "refunds", "dev")
    held_out = _task(
        "b",
        "lookups",
        "held_out",
        "Where is my keyboard order? It has not shown up yet.",
        total_minor=8000,
        status="shipped",
    )
    assert find_leaks([dev, held_out]) == []


def test_a_family_must_stay_in_one_split() -> None:
    [leak] = find_leaks(
        [
            _task("a", "refunds", "dev"),
            _task("b", "refunds", "validation", "Other words.", total_minor=1),
        ]
    )
    assert (leak.kind, leak.task_keys, leak.splits) == (
        "family_spans_splits",
        ["a", "b"],
        ["dev", "validation"],
    )


def test_a_copy_with_renamed_ids_is_caught() -> None:
    dev = _task("a", "refunds", "dev")
    [leak] = find_leaks([dev, _renamed(dev, "b", "refunds_copy", "held_out")])
    assert leak.kind == "same_fixture"


def test_a_lightly_reworded_request_is_caught() -> None:
    dev = _task("a", "refunds", "dev")
    reworded = _task(
        "b",
        "headphones",
        "held_out",
        "I sent back the headphones from order ord_1001 since the headband arrived cracked. "
        "Can I get my money back?",
        total_minor=30000,
    )
    [leak] = find_leaks([dev, reworded])
    assert leak.kind == "similar_text"


def test_near_copies_inside_one_split_are_allowed() -> None:
    dev = _task("a", "refunds", "dev")
    assert find_leaks([dev, _renamed(dev, "b", "refunds", "dev")]) == []
