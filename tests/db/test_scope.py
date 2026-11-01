import ast
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.ids import new_id
from app.db.scope import CrossWorkspaceError, NotFoundError, WorkspaceScope

APP = Path(__file__).parents[2] / "app"

# Only these modules may build queries themselves. Everything else must go through
# WorkspaceScope or a repository function, so no query can miss the workspace filter.
QUERY_MODULES = {
    "app/db",
    "app/auth",
    "app/environments/state_store.py",
    "app/execution/trace_recorder.py",
}
QUERY_BUILDERS = {"select", "insert", "update", "delete", "text"}
RAW_SESSION_CALLS = {"execute", "scalar", "scalars", "get", "get_one"}


async def test_rows_from_another_workspace_look_missing(
    sessionmaker: async_sessionmaker[AsyncSession], attempt: models.Attempt
) -> None:
    other = models.Workspace(id=new_id("ws"), name="other")
    async with sessionmaker.begin() as session:
        session.add(other)
        mine = WorkspaceScope(session, attempt.workspace_id)
        theirs = WorkspaceScope(session, other.id)
        assert (await mine.require(models.Attempt, attempt.id)).id == attempt.id
        assert await theirs.get(models.Attempt, attempt.id) is None
        with pytest.raises(NotFoundError):
            await theirs.require(models.Attempt, attempt.id)
        assert await theirs.all(theirs.query(models.Attempt)) == []


async def test_adding_a_row_for_another_workspace_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with sessionmaker() as session:
        scope = WorkspaceScope(session, workspace_id)
        with pytest.raises(CrossWorkspaceError):
            scope.add(models.Dataset(id=new_id("ds"), workspace_id="ws_other", name="x"))


def _allowed(path: Path) -> bool:
    relative = path.relative_to(APP.parent).as_posix()
    return any(relative == m or relative.startswith(m + "/") for m in QUERY_MODULES)


def _is_session(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "session") or (
        isinstance(node, ast.Attribute) and node.attr == "session"
    )


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlalchemy"):
            found += [f"imports {a.name}" for a in node.names if a.name in QUERY_BUILDERS]
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in RAW_SESSION_CALLS
            and _is_session(node.func.value)
        ):
            found.append(f"calls session.{node.func.attr}")
    return found


def test_only_the_data_layer_builds_queries() -> None:
    problems = {
        path.relative_to(APP.parent).as_posix(): v
        for path in APP.rglob("*.py")
        if not _allowed(path) and (v := _violations(path))
    }
    assert problems == {}


def test_guard_spots_raw_queries(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from sqlalchemy import select\n"
        "async def f(session, scope):\n"
        "    await scope.session.execute(1)\n"
        "    return await session.get_one(object, 1)\n"
    )
    assert sorted(_violations(sample)) == [
        "calls session.execute",
        "calls session.get_one",
        "imports select",
    ]
