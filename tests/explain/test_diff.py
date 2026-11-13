from app.explain.diff import config_diff

BASE = {
    "model": "m1",
    "prompt": "You help with orders. Always look up the order before you answer. Be brief.",
    "tools": [{"name": "get_order"}, {"name": "issue_refund", "parameters": {"a": 1}}],
    "temperature": 0,
}


def test_each_part_of_the_change_is_named() -> None:
    candidate = {
        **BASE,
        "model": "m2",
        "prompt": "You help with orders. Be brief.",
        "tools": [{"name": "issue_refund", "parameters": {"a": 2}}, {"name": "cancel"}],
        "temperature": 0.7,
    }
    changes = {c.field: c for c in config_diff(BASE, candidate)}
    assert changes["model"].summary == "model 'm1' -> 'm2'"
    assert changes["prompt"].detail == ("-Always look up the order before you answer.",)
    assert changes["prompt"].summary == "prompt: 1 sentences removed, 0 added"
    assert changes["tools"].detail == ("- get_order", "+ cancel", "~ issue_refund")
    assert changes["settings"].detail == ("temperature: 0 -> 0.7",)


def test_the_same_config_has_no_changes() -> None:
    assert config_diff(BASE, dict(BASE)) == []


def test_adding_a_sentence_reads_as_one_added() -> None:
    candidate = {**BASE, "prompt": BASE["prompt"] + " Be quick!"}
    [change] = config_diff(BASE, candidate)
    assert change.detail == ("+Be quick!",)
