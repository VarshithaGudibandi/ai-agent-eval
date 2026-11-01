from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import BaseModel
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class SchemaStatus(BaseModel):
    current: list[str]
    expected: list[str]

    @property
    def at_head(self) -> bool:
        return set(self.current) == set(self.expected)


def expected_heads() -> list[str]:
    config = Config(toml_file=str(PROJECT_ROOT / "pyproject.toml"))
    return sorted(ScriptDirectory.from_config(config).get_heads())


def _current_heads(connection: Connection) -> list[str]:
    return sorted(MigrationContext.configure(connection).get_current_heads())


async def schema_status(engine: AsyncEngine) -> SchemaStatus:
    async with engine.connect() as connection:
        current = await connection.run_sync(_current_heads)
    return SchemaStatus(current=current, expected=expected_heads())
