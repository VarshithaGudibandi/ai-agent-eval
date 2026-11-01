import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.schema_status import SchemaStatus, expected_heads, schema_status


def test_at_head_ignores_order() -> None:
    assert SchemaStatus(current=["b", "a"], expected=["a", "b"]).at_head


def test_not_at_head_when_revision_missing() -> None:
    assert not SchemaStatus(current=[], expected=["a"]).at_head


async def test_live_database_matches_migration_scripts(engine: AsyncEngine) -> None:
    result = await schema_status(engine)
    assert result.expected == expected_heads()
    assert result.at_head


async def test_unmigrated_database_reports_behind(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.db.schema_status.expected_heads", lambda: ["abc123"])
    result = await schema_status(engine)
    assert not result.at_head
