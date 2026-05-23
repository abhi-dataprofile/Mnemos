"""Unit tests for :mod:`codereview.graph.embeddings`."""

from __future__ import annotations

import pytest

from codereview.graph.embeddings import (
    EmbeddingPipeline,
    EmbeddingRequest,
    InMemoryCache,
    cache_key,
)


class RecordingProvider:
    """Test double that records batches handed to it."""

    def __init__(self, vector_dim: int = 4) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self._dim = vector_dim

    async def __call__(self, contents: list[str], model: str) -> list[list[float]]:
        self.calls.append((list(contents), model))
        # Deterministic vector per content + model so tests can assert on equality.
        return [
            [float(len(c)), float(sum(map(ord, model)) % 97)] + [0.0] * (self._dim - 2)
            for c in contents
        ]


# -- cache_key --------------------------------------------------------------


def test_cache_key_is_deterministic() -> None:
    a = cache_key("model-x", "hello world")
    b = cache_key("model-x", "hello world")
    assert a == b


def test_cache_key_differs_by_model() -> None:
    assert cache_key("a", "same content") != cache_key("b", "same content")


def test_cache_key_differs_by_content() -> None:
    assert cache_key("m", "x") != cache_key("m", "y")


# -- Pipeline batching + caching -------------------------------------------


@pytest.mark.asyncio
async def test_batches_fill_at_configured_size_and_flush_once() -> None:
    symbol = RecordingProvider()
    pipe = EmbeddingPipeline(symbol_provider=symbol, batch_size=3)

    for i in range(3):
        await pipe.submit(EmbeddingRequest(kind="symbol", content=f"c{i}", tag=f"t{i}"))

    # A single batch should have been flushed on hitting the size threshold.
    assert len(symbol.calls) == 1
    # The pipeline has already emitted 3 results; flush() is a no-op here.
    results = await pipe.flush()
    assert len(results) == 3
    assert [r.tag for r in results] == ["t0", "t1", "t2"]
    assert pipe.stats.api_calls == 1
    assert pipe.stats.cache_hits == 0
    assert pipe.stats.items_embedded == 3


@pytest.mark.asyncio
async def test_flush_drains_partial_batches() -> None:
    symbol = RecordingProvider()
    pipe = EmbeddingPipeline(symbol_provider=symbol, batch_size=100)
    await pipe.submit(EmbeddingRequest(kind="symbol", content="only", tag="tag"))
    assert symbol.calls == []  # not flushed yet

    results = await pipe.flush()
    assert len(symbol.calls) == 1
    assert [r.tag for r in results] == ["tag"]


@pytest.mark.asyncio
async def test_cache_hit_skips_provider() -> None:
    cache = InMemoryCache()
    # Prime the cache for an exact match on ("voyage-code-3", "hello").
    cache.put(cache_key("voyage-code-3", "hello"), [1.0, 2.0, 3.0])

    symbol = RecordingProvider()
    pipe = EmbeddingPipeline(symbol_provider=symbol, cache=cache, batch_size=10)
    await pipe.submit(EmbeddingRequest(kind="symbol", content="hello", tag="h"))
    results = await pipe.flush()

    assert symbol.calls == []
    assert len(results) == 1
    assert results[0].cache_hit is True
    assert results[0].vector == [1.0, 2.0, 3.0]
    assert pipe.stats.cache_hits == 1
    assert pipe.stats.api_calls == 0


@pytest.mark.asyncio
async def test_provider_results_repopulate_cache() -> None:
    cache = InMemoryCache()
    symbol = RecordingProvider()
    pipe = EmbeddingPipeline(symbol_provider=symbol, cache=cache, batch_size=10)

    await pipe.submit(EmbeddingRequest(kind="symbol", content="x", tag="t"))
    await pipe.flush()
    assert len(cache) == 1

    # Second pipeline reusing the same cache should never hit the provider.
    symbol2 = RecordingProvider()
    pipe2 = EmbeddingPipeline(symbol_provider=symbol2, cache=cache, batch_size=10)
    await pipe2.submit(EmbeddingRequest(kind="symbol", content="x", tag="t2"))
    results = await pipe2.flush()
    assert symbol2.calls == []
    assert results[0].cache_hit is True


@pytest.mark.asyncio
async def test_symbol_and_prose_use_different_providers_and_models() -> None:
    symbol = RecordingProvider()
    prose = RecordingProvider()
    pipe = EmbeddingPipeline(
        symbol_provider=symbol,
        prose_provider=prose,
        symbol_model="voyage-code-3",
        prose_model="text-embedding-3-large",
        batch_size=5,
    )
    await pipe.submit(EmbeddingRequest(kind="symbol", content="def f(): pass", tag="s"))
    await pipe.submit(EmbeddingRequest(kind="prose", content="fixes #42", tag="p"))
    await pipe.flush()

    assert symbol.calls and symbol.calls[0][1] == "voyage-code-3"
    assert prose.calls and prose.calls[0][1] == "text-embedding-3-large"
    # And vectors differ because the models hash to different magic numbers.
    results_by_tag = {r.tag: r.vector for r in pipe._results}  # noqa: SLF001
    assert results_by_tag["s"] != results_by_tag["p"]


@pytest.mark.asyncio
async def test_missing_provider_for_kind_raises() -> None:
    # Prose-only pipeline: submitting a symbol request on flush should raise.
    pipe = EmbeddingPipeline(prose_provider=RecordingProvider(), batch_size=5)
    await pipe.submit(EmbeddingRequest(kind="symbol", content="x", tag="s"))
    with pytest.raises(RuntimeError, match="No embedding provider"):
        await pipe.flush()


@pytest.mark.asyncio
async def test_provider_output_length_mismatch_raises() -> None:
    async def bad_provider(_contents: list[str], _model: str) -> list[list[float]]:
        return [[1.0]]  # returns too few

    pipe = EmbeddingPipeline(symbol_provider=bad_provider, batch_size=5)
    await pipe.submit(EmbeddingRequest(kind="symbol", content="a", tag="a"))
    await pipe.submit(EmbeddingRequest(kind="symbol", content="b", tag="b"))
    with pytest.raises(RuntimeError, match="returned 1 vectors for 2 inputs"):
        await pipe.flush()


@pytest.mark.asyncio
async def test_batch_size_must_be_positive() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        EmbeddingPipeline(batch_size=0)


@pytest.mark.asyncio
async def test_stats_counted_by_kind() -> None:
    pipe = EmbeddingPipeline(
        symbol_provider=RecordingProvider(),
        prose_provider=RecordingProvider(),
        batch_size=10,
    )
    await pipe.submit(EmbeddingRequest(kind="symbol", content="a", tag="1"))
    await pipe.submit(EmbeddingRequest(kind="symbol", content="b", tag="2"))
    await pipe.submit(EmbeddingRequest(kind="prose", content="c", tag="3"))
    await pipe.flush()

    assert pipe.stats.by_kind == {"symbol": 2, "prose": 1}
    assert pipe.stats.api_calls == 2
    assert pipe.stats.items_embedded == 3
