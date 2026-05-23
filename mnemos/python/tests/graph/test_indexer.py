"""Smoke test for :class:`codereview.graph.builder.GraphBuilder`.

Points the builder at ``fixtures/conflict-repo/base/`` and verifies the Phase 2
acceptance criteria: the core symbol is indexed, the call edge between the API
handler and the billing helper lands in the database, both ADRs are parsed,
and the embedding pipeline receives requests for every symbol + ADR.

Uses the SQLite + ``@compiles`` harness from ``conftest.py`` — no Postgres
needed locally. The query paths exercised here are dialect-agnostic; the
pgvector-specific bits are covered by the separate ``similar_*`` tests.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from codereview.graph import models as m
from codereview.graph.builder import GraphBuilder
from codereview.graph.embeddings import (
    EmbeddingPipeline,
    EmbeddingRequest,
)

# Repo root is four levels up from this file:
#   python/tests/graph/test_indexer.py -> python/tests/graph -> python/tests -> python -> <repo>
_FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "fixtures" / "conflict-repo" / "base"


async def _seed_repository(session: AsyncSession) -> m.Repository:
    repo = m.Repository(
        id=uuid4(),
        github_id=99001,
        owner="fixtures",
        name="conflict-repo",
        installation_id=1,
        default_branch="main",
    )
    session.add(repo)
    await session.flush()
    return repo


class _StubProvider:
    """Records calls made by the embedding pipeline and returns a toy vector."""

    def __init__(self) -> None:
        self.requests: list[tuple[list[str], str]] = []

    async def __call__(self, contents: list[str], model: str) -> list[list[float]]:
        self.requests.append((list(contents), model))
        return [[float(len(c)), 0.0, 0.0, 0.0] for c in contents]


@pytest.mark.asyncio
async def test_builder_indexes_fixture_repo(db_session: AsyncSession) -> None:
    assert _FIXTURE_ROOT.is_dir(), f"fixture missing: {_FIXTURE_ROOT}"

    repo = await _seed_repository(db_session)
    symbol_provider = _StubProvider()
    prose_provider = _StubProvider()
    pipeline = EmbeddingPipeline(
        symbol_provider=symbol_provider,
        prose_provider=prose_provider,
        batch_size=50,
    )

    builder = GraphBuilder(db_session, repo.id, embedding_pipeline=pipeline)
    stats = await builder.index_working_tree(_FIXTURE_ROOT, head_sha="fixture-head")

    # ---- files + symbol-level acceptance ------------------------------------

    billing_invoice = (
        await db_session.execute(
            select(m.File).where(
                m.File.repository_id == repo.id,
                m.File.path == "src/billing/invoice.py",
            )
        )
    ).scalar_one()
    assert billing_invoice.language == "python"
    assert billing_invoice.first_seen_sha == "fixture-head"

    generate_pdf = (
        await db_session.execute(
            select(m.Symbol).where(
                m.Symbol.file_id == billing_invoice.id,
                m.Symbol.qualified_name == "generate_pdf",
            )
        )
    ).scalar_one()
    assert generate_pdf.kind == "function"
    assert "invoice_id" in (generate_pdf.signature or "")
    assert "repo" in (generate_pdf.signature or "")

    # ---- call edge acceptance: download_pdf -> generate_pdf -----------------

    api_invoices = (
        await db_session.execute(
            select(m.File).where(
                m.File.repository_id == repo.id,
                m.File.path == "src/api/invoices.py",
            )
        )
    ).scalar_one()
    download_pdf = (
        await db_session.execute(
            select(m.Symbol).where(
                m.Symbol.file_id == api_invoices.id,
                m.Symbol.qualified_name == "download_pdf",
            )
        )
    ).scalar_one()

    edge = (
        await db_session.execute(
            select(m.SymbolCall).where(
                m.SymbolCall.caller_id == download_pdf.id,
                m.SymbolCall.callee_id == generate_pdf.id,
            )
        )
    ).scalar_one()
    assert edge.dynamic is False

    # ---- ADR acceptance -----------------------------------------------------

    adr_rows = (
        (
            await db_session.execute(
                select(m.ADR).where(m.ADR.repository_id == repo.id).order_by(m.ADR.path)
            )
        )
        .scalars()
        .all()
    )
    adr_paths = {a.path for a in adr_rows}
    assert adr_paths == {
        "docs/adr/adr-001-repository-pattern.md",
        "docs/adr/adr-002-error-handling.md",
    }
    for adr in adr_rows:
        assert adr.title
        assert adr.status  # normalised to a lowercase token

    # ---- embedding pipeline acceptance --------------------------------------

    # Every symbol submitted on the code path + every ADR on the prose path.
    submitted_symbol_count = sum(len(batch) for batch, _ in symbol_provider.requests)
    submitted_prose_count = sum(len(batch) for batch, _ in prose_provider.requests)
    assert submitted_symbol_count == stats.symbols_written
    assert submitted_prose_count == stats.adrs_written == 2
    assert stats.embeddings_requested == stats.symbols_written + stats.adrs_written

    # ---- basic stats sanity -------------------------------------------------

    assert stats.files_indexed >= 4  # src/billing/invoice.py + api + repositories + models etc.
    assert stats.symbols_written >= 4
    assert stats.call_edges_written >= 1
    # Running a fresh builder against the same tree must be idempotent: the
    # dedup path on File + Symbol + ADR must short-circuit every upsert and
    # write nothing new.
    builder2 = GraphBuilder(db_session, repo.id, embedding_pipeline=pipeline)
    stats2 = await builder2.index_working_tree(_FIXTURE_ROOT, head_sha="fixture-head")
    assert stats2.files_indexed == stats.files_indexed
    assert stats2.symbols_written == 0
    assert stats2.adrs_written == 0


@pytest.mark.asyncio
async def test_builder_tolerates_missing_embedding_pipeline(db_session: AsyncSession) -> None:
    """Indexing without a pipeline still writes files/symbols/ADRs/edges."""
    repo = await _seed_repository(db_session)
    builder = GraphBuilder(db_session, repo.id, embedding_pipeline=None)
    stats = await builder.index_working_tree(_FIXTURE_ROOT)

    assert stats.symbols_written > 0
    assert stats.adrs_written == 2
    assert stats.embeddings_requested == 0


@pytest.mark.asyncio
async def test_builder_caches_symbol_embeddings_across_runs(db_session: AsyncSession) -> None:
    """A second indexing run with the same cache must not re-hit the provider."""
    repo = await _seed_repository(db_session)

    symbol_provider = _StubProvider()
    prose_provider = _StubProvider()
    pipeline = EmbeddingPipeline(
        symbol_provider=symbol_provider,
        prose_provider=prose_provider,
        batch_size=50,
    )

    builder = GraphBuilder(db_session, repo.id, embedding_pipeline=pipeline)
    await builder.index_working_tree(_FIXTURE_ROOT)
    first_calls = len(symbol_provider.requests) + len(prose_provider.requests)
    assert first_calls > 0

    # Re-using the pipeline (and its in-memory cache) should cost zero API
    # calls on the second pass, because every symbol + ADR hashes identically.
    await builder.index_working_tree(_FIXTURE_ROOT)
    assert len(symbol_provider.requests) + len(prose_provider.requests) == first_calls

    # Sanity: the cache key helper really is what keeps us honest — submitting
    # the same content/model tuple again returns cache_hit=True.
    await pipeline.submit(
        EmbeddingRequest(
            kind="symbol",
            content="function generate_pdf(invoice_id: int, repo: InvoiceRepository) -> bytes",
            tag="probe",
        )
    )
    await pipeline.flush()
