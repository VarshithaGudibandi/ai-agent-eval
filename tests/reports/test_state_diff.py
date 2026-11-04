from app.reports.state_diff import state_diff


def test_added_removed_and_changed_records_are_told_apart() -> None:
    initial = {
        "orders": [{"order_id": "o1", "status": "delivered"}, {"order_id": "o2", "status": "x"}],
        "refunds": [],
    }
    final = {
        "orders": [{"order_id": "o1", "status": "refunded"}],
        "refunds": [{"refund_id": "r1", "order_id": "o1", "amount_minor": 500}],
    }
    changes = {(c.collection, c.record_id): c for c in state_diff(initial, final)}
    assert changes[("orders", "o1")].change == "changed"
    [field] = changes[("orders", "o1")].fields
    assert (field.field, field.before, field.after) == ("status", "delivered", "refunded")
    assert changes[("orders", "o2")].change == "removed"
    assert changes[("refunds", "r1")].change == "added"
    assert changes[("refunds", "r1")].record["amount_minor"] == 500


def test_unchanged_state_has_no_changes() -> None:
    state = {"orders": [{"order_id": "o1"}]}
    assert state_diff(state, state) == []


def test_records_without_an_id_are_matched_by_content() -> None:
    changes = state_diff({"notes": [{"text": "a"}]}, {"notes": [{"text": "b"}]})
    assert sorted(c.change for c in changes) == ["added", "removed"]
