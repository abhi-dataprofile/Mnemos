"""Unit tests for :class:`codereview.graph.incremental.IncrementalUpdater`.

Tests mutate a throwaway working tree under :func:`tmp_path` so we can freely
add / modify / delete files without touching the fixture repo.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from codereview.graph import models as m
from codereview.graph.builder import GraphBuilder
from codereview.graph.embeddings import EmbeddingPipeline
from codereview.graph.incremental import (
    FileChange,
    IncrementalUpdater,
)


async def _seed_repository(session: AsyncSession) -> m.Repository:
    repo = m.Repository(
        id=uuid4(),
        github_id=77002,
        owner="fixtures",
        name="incremental-repo",
        installation_id=1,
        default_branch="main",
    )
    session.add(repo)
    await session.flush()
    return repo


class _StubProvider:
    def __init__(self) -> None:
        self.requests: list[tuple[list[str], str]] = []

    async def __call__(self, contents: list[str], model: str) -> list[list[float]]:
        self.requests.append((list(contents), model))
        return [[float(len(c)), 0.0, 0.0, 0.0] for c in contents]


def _make_initial_tree(root: Path) -> None:
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "a.py").write_text("def foo():\n    return 1\n")
    (root / "pkg" / "b.py").write_text("from .a import foo\n\ndef bar():\n    return foo()\n")


async def _initial_index(session: AsyncSession, repo: m.Repository, root: Path) -> None:
    builder = GraphBuilder(session, repo.id)
    await builder.index_working_tree(root, head_sha="sha-initial")


# -- Core flows ------------------------------------------------------------


@pytest.mark.asyncio
async def test_modified_file_reindexes_symbols_in_place(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    repo = await _seed_repository(db_session)
    _make_initial_tree(tmp_path)
    await _initial_index(db_session, repo, tmp_path)

    # Capture Symbol.id for bar so we can assert stability across the edit.
    bar_before = (
        await db_session.execute(select(m.Symbol).where(m.Symbol.qualified_name == "bar"))
    ).scalar_one()
    bar_id_before = bar_before.id

    # Edit b.py: change bar's body but keep its signature.
    (tmp_path / "pkg" / "b.py").write_text(
        "from .a import foo\n\ndef bar():\n    # extra logic\n    return foo() + 1\n"
    )

    updater = IncrementalUpdater(db_session, repo.id)
    stats = await updater.apply(
        tmp_path,
        [FileChange(path="pkg/b.py", kind="M")],
        head_sha="sha-head",
    )

    assert stats.files_modified == 1
    assert stats.symbols_written == 1  # bar re-upserted in place
    assert stats.symbols_deleted == 0

    bar_after = (
        await db_session.execute(select(m.Symbol).where(m.Symbol.qualified_name == "bar"))
    ).scalar_one()
    assert bar_after.id == bar_id_before  # in-place update preserves id

    # The outgoing edge bar -> foo must have been re-emitted against the
    # existing foo symbol (Symbol.id for foo stayed stable too).
    foo_row = (
        await db_session.execute(select(m.Symbol).where(m.Symbol.qualified_name == "foo"))
    ).scalar_one()
    edges = (
        (
            await db_session.execute(
                select(m.SymbolCall).where(m.SymbolCall.caller_id == bar_after.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(edges) == 1
    assert edges[0].callee_id == foo_row.id

    repo_after = (
        await db_session.execute(select(m.Repository).where(m.Repository.id == repo.id))
    ).scalar_one()
    assert repo_after.last_indexed_sha == "sha-head"


@pytest.mark.asyncio
async def test_deleted_file_removes_symbols_and_edges(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    repo = await _seed_repository(db_session)
    _make_initial_tree(tmp_path)
    await _initial_index(db_session, repo, tmp_path)

    # Sanity: edges exist before we delete a.py.
    pre_edges = (await db_session.execute(select(m.SymbolCall))).scalars().all()
    assert len(pre_edges) == 1

    (tmp_path / "pkg" / "a.py").unlink()

    updater = IncrementalUpdater(db_session, repo.id)
    stats = await updater.apply(
        tmp_path,
        [FileChange(path="pkg/a.py", kind="D")],
        head_sha="sha-del",
    )

    assert stats.files_removed == 1
    assert stats.symbols_deleted >= 1

    # foo symbol and the edge pointing at it are gone via FK cascade.
    foo_rows = (
        (await db_session.execute(select(m.Symbol).where(m.Symbol.qualified_name == "foo")))
        .scalars()
        .all()
    )
    assert foo_rows == []
    remaining_edges = (await db_session.execute(select(m.SymbolCall))).scalars().all()
    assert remaining_edges == []


@pytest.mark.asyncio
async def test_added_file_creates_row_and_resolves_calls(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    repo = await _seed_repository(db_session)
    _make_initial_tree(tmp_path)
    await _initial_index(db_session, repo, tmp_path)

    (tmp_path / "pkg" / "c.py").write_text("from .a import foo\n\ndef baz():\n    return foo()\n")

    pipeline = EmbeddingPipeline(symbol_provider=_StubProvider(), batch_size=10)
    updater = IncrementalUpdater(db_session, repo.id, embedding_pipeline=pipeline)
    stats = await updater.apply(
        tmp_path,
        [FileChange(path="pkg/c.py", kind="A")],
        head_sha="sha-add",
    )

    assert stats.files_added == 1
    assert stats.symbols_written == 1
    assert stats.embeddings_requested == 1

    baz = (
        await db_session.execute(select(m.Symbol).where(m.Symbol.qualified_name == "baz"))
    ).scalar_one()
    foo = (
        await db_session.execute(select(m.Symbol).where(m.Symbol.qualified_name == "foo"))
    ).scalar_one()
    edge = (
        await db_session.execute(select(m.SymbolCall).where(m.SymbolCall.caller_id == baz.id))
    ).scalar_one()
    assert edge.callee_id == foo.id


@pytest.mark.asyncio
async def test_removed_symbol_within_modified_file_is_pruned(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    repo = await _seed_repository(db_session)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "x.py").write_text("def alpha():\n    pass\n\ndef beta():\n    pass\n")
    await _initial_index(db_session, repo, tmp_path)

    # Rewrite x.py to drop beta.
    (tmp_path / "pkg" / "x.py").write_text("def alpha():\n    return 42\n")

    updater = IncrementalUpdater(db_session, repo.id)
    stats = await updater.apply(
        tmp_path,
        [FileChange(path="pkg/x.py", kind="M")],
        head_sha="sha-shrink",
    )

    assert stats.symbols_deleted == 1
    remaining = (
        (
            await db_session.execute(
                select(m.Symbol.qualified_name).where(m.Symbol.repository_id == repo.id)
            )
        )
        .scalars()
        .all()
    )
    assert "alpha" in remaining
    assert "beta" not in remaining


@pytest.mark.asyncio
async def test_non_source_changes_are_ignored(db_session: AsyncSession, tmp_path: Path) -> None:
    repo = await _seed_repository(db_session)
    _make_initial_tree(tmp_path)
    await _initial_index(db_session, repo, tmp_path)

    updater = IncrementalUpdater(db_session, repo.id)
    # README changes, config tweaks — nothing the parser registry claims.
    stats = await updater.apply(
        tmp_path,
        [FileChange(path="README.md", kind="M"), FileChange(path="pyproject.toml", kind="M")],
        head_sha="sha-readme",
    )

    assert stats.files_modified == 0
    assert stats.files_added == 0
    assert stats.files_removed == 0
    assert stats.symbols_written == 0
    # But last_indexed_sha still advances so the next run starts from here.
    repo_after = (
        await db_session.execute(select(m.Repository).where(m.Repository.id == repo.id))
    ).scalar_one()
    assert repo_after.last_indexed_sha == "sha-readme"


@pytest.mark.asyncio
async def test_no_changes_advances_last_indexed_sha(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    repo = await _seed_repository(db_session)
    _make_initial_tree(tmp_path)
    await _initial_index(db_session, repo, tmp_path)

    updater = IncrementalUpdater(db_session, repo.id)
    stats = await updater.apply(tmp_path, [], head_sha="sha-noop")

    assert stats.files_modified == 0
    assert stats.call_edges_written == 0
    repo_after = (
        await db_session.execute(select(m.Repository).where(m.Repository.id == repo.id))
    ).scalar_one()
    assert repo_after.last_indexed_sha == "sha-noop"


# -- ADR re-ingestion -------------------------------------------------------


def _adr_text(title: str, status: str, body: str = "Sample context.") -> str:
    return f"# {title}\n\nStatus: {status}\n\n## Context\n{body}\n\n## Decision\nDo the thing.\n"


def _write_adr(root: Path, rel_path: str, text: str) -> None:
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


@pytest.mark.asyncio
async def test_adr_add_creates_row_and_submits_embedding(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    repo = await _seed_repository(db_session)
    _make_initial_tree(tmp_path)
    await _initial_index(db_session, repo, tmp_path)

    # Initial index saw no ADRs because the tree had none.
    pre = (await db_session.execute(select(m.ADR))).scalars().all()
    assert pre == []

    _write_adr(tmp_path, "docs/adr/adr-001.md", _adr_text("ADR 1: Example", "accepted"))

    pipeline = EmbeddingPipeline(prose_provider=_StubProvider(), batch_size=10)
    updater = IncrementalUpdater(db_session, repo.id, embedding_pipeline=pipeline)
    stats = await updater.apply(
        tmp_path,
        [FileChange(path="docs/adr/adr-001.md", kind="A")],
        head_sha="sha-adr-add",
    )

    assert stats.adrs_written == 1
    assert stats.adrs_deleted == 0
    assert stats.embeddings_requested == 1

    rows = (await db_session.execute(select(m.ADR))).scalars().all()
    assert len(rows) == 1
    assert rows[0].path == "docs/adr/adr-001.md"
    assert rows[0].title == "ADR 1: Example"
    assert rows[0].status == "accepted"


@pytest.mark.asyncio
async def test_adr_modify_updates_in_place(db_session: AsyncSession, tmp_path: Path) -> None:
    repo = await _seed_repository(db_session)
    _make_initial_tree(tmp_path)
    _write_adr(tmp_path, "docs/adr/adr-002.md", _adr_text("ADR 2", "proposed"))
    await _initial_index(db_session, repo, tmp_path)

    before = (await db_session.execute(select(m.ADR))).scalar_one()
    before_id = before.id

    # Flip status to accepted and edit the body.
    _write_adr(
        tmp_path,
        "docs/adr/adr-002.md",
        _adr_text("ADR 2 revised", "accepted", body="Revised rationale."),
    )

    updater = IncrementalUpdater(db_session, repo.id)
    stats = await updater.apply(
        tmp_path,
        [FileChange(path="docs/adr/adr-002.md", kind="M")],
        head_sha="sha-adr-mod",
    )

    # An in-place update must not bump adrs_written.
    assert stats.adrs_written == 0
    assert stats.adrs_deleted == 0

    after = (await db_session.execute(select(m.ADR))).scalar_one()
    assert after.id == before_id
    assert after.title == "ADR 2 revised"
    assert after.status == "accepted"
    assert "Revised rationale" in after.body


@pytest.mark.asyncio
async def test_adr_delete_removes_row(db_session: AsyncSession, tmp_path: Path) -> None:
    repo = await _seed_repository(db_session)
    _make_initial_tree(tmp_path)
    _write_adr(tmp_path, "docs/adr/adr-003.md", _adr_text("ADR 3", "accepted"))
    await _initial_index(db_session, repo, tmp_path)

    assert (await db_session.execute(select(m.ADR))).scalars().all() != []

    (tmp_path / "docs" / "adr" / "adr-003.md").unlink()

    updater = IncrementalUpdater(db_session, repo.id)
    stats = await updater.apply(
        tmp_path,
        [FileChange(path="docs/adr/adr-003.md", kind="D")],
        head_sha="sha-adr-del",
    )

    assert stats.adrs_deleted == 1
    assert stats.adrs_written == 0
    assert (await db_session.execute(select(m.ADR))).scalars().all() == []


@pytest.mark.asyncio
async def test_adr_losing_status_line_is_treated_as_delete(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """If an edit strips the ADR structure, the row should drop out too."""
    repo = await _seed_repository(db_session)
    _make_initial_tree(tmp_path)
    _write_adr(tmp_path, "docs/adr/adr-004.md", _adr_text("ADR 4", "accepted"))
    await _initial_index(db_session, repo, tmp_path)

    # Rewrite to a plain note — no Status: line, so parse_adr returns None.
    (tmp_path / "docs" / "adr" / "adr-004.md").write_text(
        "# Just a note\n\nNothing ADR-shaped here.\n"
    )

    updater = IncrementalUpdater(db_session, repo.id)
    stats = await updater.apply(
        tmp_path,
        [FileChange(path="docs/adr/adr-004.md", kind="M")],
        head_sha="sha-adr-strip",
    )

    assert stats.adrs_deleted == 1
    assert stats.adrs_written == 0
    assert (await db_session.execute(select(m.ADR))).scalars().all() == []
