import json
from typing import Any

from pydantic import BaseModel

# What an attempt changed in its environment: the initial snapshot against the final one.


class FieldChange(BaseModel):
    field: str
    before: Any
    after: Any


class RecordChange(BaseModel):
    collection: str
    record_id: str
    change: str  # added, removed or changed
    record: dict[str, Any]
    fields: list[FieldChange]


def _record_id(record: dict[str, Any]) -> str:
    # Records lead with their own id, such as order_id or refund_id. A record without one is
    # identified by its whole content, so it can only appear or disappear.
    for key, value in record.items():
        if key.endswith("_id"):
            return str(value)
    return json.dumps(record, sort_keys=True)


def _index(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {_record_id(r): r for r in records}


def state_diff(initial: dict[str, Any], final: dict[str, Any]) -> list[RecordChange]:
    changes: list[RecordChange] = []
    for collection in sorted(set(initial) | set(final)):
        before = _index(initial.get(collection) or [])
        after = _index(final.get(collection) or [])
        for record_id in sorted(set(before) | set(after)):
            old, new = before.get(record_id), after.get(record_id)
            if old is None and new is not None:
                changes.append(
                    RecordChange(
                        collection=collection,
                        record_id=record_id,
                        change="added",
                        record=new,
                        fields=[],
                    )
                )
            elif new is None and old is not None:
                changes.append(
                    RecordChange(
                        collection=collection,
                        record_id=record_id,
                        change="removed",
                        record=old,
                        fields=[],
                    )
                )
            elif old is not None and new is not None and old != new:
                fields = [
                    FieldChange(field=key, before=old.get(key), after=new.get(key))
                    for key in sorted(set(old) | set(new))
                    if old.get(key) != new.get(key)
                ]
                changes.append(
                    RecordChange(
                        collection=collection,
                        record_id=record_id,
                        change="changed",
                        record=new,
                        fields=fields,
                    )
                )
    return changes
