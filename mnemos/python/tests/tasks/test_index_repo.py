"""Unit tests for the ``index_repo`` CLI.

These exercise the async ``_run`` helper directly so we can inject the
SQLite test session factory instead of spinning up a real Postgres. The
argparse + ``asyncio.run`` wrapper around ``_run`` is covered by a smoke
import test at the bottom of the file.
"""

from __future__ import annotations

from argparse import Namespace
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from pgvector.sqlalchemy import Vector
from sqlalchemy import event, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

import codereview.graph.models  # noqa: F401 — register tables on Base.metadata
from codereview.db import Base
from codereview.graph import models as m
from codereview.tasks import index_repo as index_repo_mod


# Mirror the SQLite compatibility layer from tests/graph/conftest.py so the
# CLI can run against an in-memory DB. These compilers are global to
# SQLAlchemy's registry — redefining them here is a no-op but harmless.
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):  # type: ignore[no-untyped-def]
    return "TEXT"


@compiles(Vector, "sqlite")
def _compile_vector_sqlite(_type, _compiler, **_kw):  # type: ignore[no-untyped-def]
    return "TEXT"


@pytest_asyncio.fixture
async def sqlite_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield factory
    finally:
        await engine.dispose()


def _make_tree(root: Path) -> None:
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "a.py").write_text("def foo():\n    return 1\n")
    (root / "pkg" / "b.py").write_text("from .a import foo\n\ndef bar():\n    return foo()\n")
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "docs" / "adr" / "adr-001.md").write_text(
        "# ADR 1: Example\n\nStatus: accepted\n\n## Context\nSample.\n\n## Decision\nDo the thing.\n"
    )


def _args_for(root: Path) -> Namespace:
    return Namespace(
        path=root,
        owner="local",
        name=None,
        github_id=0,
        installation_id=0,
        default_branch="main",
        head_sha="working-tree",
        with_embeddings=False,
    )


# -- happy path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_indexes_and_persists(
    tmp_path: Path,
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_tree(tmp_path)

    # Point the CLI at our in-memory session factory + stub the engine
    # dispose so the happy path doesn't accidentally create a real engine.
    monkeypatch.setattr(index_repo_mod, "get_session_factory", lambda: sqlite_factory)

    stats = await index_repo_mod._run(_args_for(tmp_path))

    assert stats.symbols_written >= 2  # foo + bar
    assert stats.call_edges_written >= 1
    assert stats.adrs_written == 1

    # Repository row was created with the directory basename as the name.
    async with sqlite_factory() as session:
        repo = (
            await session.execute(select(m.Repository).where(m.Repository.owner == "local"))
        ).scalar_one()
        assert repo.name == tmp_path.name

        symbols = (
            (await session.execute(select(m.Symbol).where(m.Symbol.repository_id == repo.id)))
            .scalars()
            .all()
        )
        qnames = {s.qualified_name for s in symbols}
        assert {"foo", "bar"}.issubset(qnames)


@pytest.mark.asyncio
async def test_run_reuses_existing_repository_row(
    tmp_path: Path,
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second run against the same tree must not create a duplicate repo."""
    _make_tree(tmp_path)
    monkeypatch.setattr(index_repo_mod, "get_session_factory", lambda: sqlite_factory)

    await index_repo_mod._run(_args_for(tmp_path))
    await index_repo_mod._run(_args_for(tmp_path))

    async with sqlite_factory() as session:
        rows = (
            (await session.execute(select(m.Repository).where(m.Repository.owner == "local")))
            .scalars()
            .all()
        )
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_run_rejects_missing_path(
    tmp_path: Path,
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(index_repo_mod, "get_session_factory", lambda: sqlite_factory)
    args = _args_for(tmp_path / "does-not-exist")
    with pytest.raises(SystemExit, match="not a directory"):
        await index_repo_mod._run(args)


@pytest.mark.asyncio
async def test_run_rejects_with_embeddings_flag(
    tmp_path: Path,
    sqlite_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_tree(tmp_path)
    monkeypatch.setattr(index_repo_mod, "get_session_factory", lambda: sqlite_factory)
    args = _args_for(tmp_path)
    args.with_embeddings = True
    with pytest.raises(SystemExit, match="not yet wired"):
        await index_repo_mod._run(args)


# -- CLI smoke -------------------------------------------------------------


def test_argparse_rejects_missing_path() -> None:
    parser = index_repo_mod._build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])  # --path is required
