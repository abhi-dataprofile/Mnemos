"""SQLite-backed test harness for the graph package.

The real Mnemos schema targets Postgres + pgvector. For unit tests we stand
up an in-memory SQLite database and register dialect compilers for the two
Postgres-specific column types the ORM uses (``JSONB`` and ``Vector``). This
lets every ``select()`` in :class:`codereview.graph.client.GraphClient` run
against a real SQLAlchemy session without needing Postgres.

pgvector similarity operators (``<=>`` and friends) do not compile under
SQLite; tests that exercise :meth:`GraphClient.similar_prs` /
:meth:`similar_adrs` verify the rendered SQL shape instead and are marked
with ``pytest.mark.pgvector``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# pgvector's Vector type only needs to render for DDL; we substitute TEXT under
# SQLite so ``create_all`` works. Tests that assert on pgvector-specific SQL
# use the raw dialect without creating the table at all.
from pgvector.sqlalchemy import Vector
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

# Importing the models module registers every table on ``Base.metadata``.
import codereview.graph.models  # noqa: F401
from codereview.db import Base


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):  # type: ignore[no-untyped-def]
    return "TEXT"


@compiles(Vector, "sqlite")
def _compile_vector_sqlite(_type, _compiler, **_kw):  # type: ignore[no-untyped-def]
    return "TEXT"


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` bound to a fresh in-memory SQLite DB.

    SQLite does not enforce foreign keys unless ``PRAGMA foreign_keys = ON``
    is issued on every connection. We wire it up here so FK cascade semantics
    behave like Postgres — without this the incremental updater's
    delete-then-cascade path silently leaves orphan rows.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "pgvector: test requires a Postgres + pgvector database (skipped under SQLite)",
    )
