import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.datasets import CaseData, CaseKeysClashError, add_cases, cases_of, save_version
from app.db.scope import WorkspaceScope

Sessions = async_sessionmaker[AsyncSession]
CASES = [
    CaseData("refund-1", {"message": "My headphones arrived broken"}, {"refund": 249.99}),
    CaseData("lookup-1", {"message": "Where is my order?"}, None, {"category": "lookup"}),
]


async def test_the_same_cases_reuse_the_newest_version(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        first, made = await save_version(scope, "support", CASES)
        again, made_again = await save_version(scope, "support", CASES)
        changed, made_changed = await save_version(scope, "support", CASES[:1])
    assert (made, made_again, made_changed) == (True, False, True)
    assert again.id == first.id
    assert (first.version, changed.version) == (1, 2)


async def test_cases_keep_their_order_and_content(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        version, _ = await save_version(scope, "support", CASES)
        rows = await cases_of(scope, version.id)
    assert [r.case_key for r in rows] == ["refund-1", "lookup-1"]
    assert (rows[0].expected, rows[1].expected, rows[1].tags) == (
        {"refund": 249.99},
        None,
        {"category": "lookup"},
    )


async def test_added_cases_make_a_new_version_after_the_old_ones(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        first, _ = await save_version(scope, "support", CASES)
        second, made = await add_cases(scope, first.dataset_id, [CaseData("new-1", {"m": "hi"})])
        rows = await cases_of(scope, second.id)
        old_rows = await cases_of(scope, first.id)
    assert made and second.version == 2
    assert [r.case_key for r in rows] == ["refund-1", "lookup-1", "new-1"]
    assert len(old_rows) == 2


async def test_case_keys_must_be_unique_in_a_version(
    sessionmaker: Sessions, workspace_id: str
) -> None:
    async with sessionmaker.begin() as session:
        scope = WorkspaceScope(session, workspace_id)
        with pytest.raises(CaseKeysClashError):
            await save_version(scope, "support", [CASES[0], CASES[0]])
        first, _ = await save_version(scope, "support", CASES)
        with pytest.raises(CaseKeysClashError):
            await add_cases(scope, first.dataset_id, [CASES[1]])
