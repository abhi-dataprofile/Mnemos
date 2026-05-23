"""Embedding pipeline.

Centralizes every embedding call made by the indexer. Two content types get
different providers:

- ``"symbol"`` — code embeddings (default: ``voyage-code-3``).
- ``"prose"`` — PR titles/bodies and ADR text (default: ``text-embedding-3-large``).

Two invariants keep the pipeline cheap and deterministic:

1. **Deterministic cache**: every ``(model, sha256(content))`` tuple maps to at
   most one embedding. A cache hit returns instantly without an API call.
   The default in-memory cache is process-local; deployments wanting to share
   the cache across workers can pass a custom :class:`EmbeddingCache`.
2. **Bounded batching**: the pipeline flushes a batch once either the size
   limit is hit (default 100) or the caller finalizes the run with
   :meth:`EmbeddingPipeline.flush`.

Providers are asynchronous and return a list of vectors (one per input). If
the caller does not supply a provider the pipeline raises on the first API
call — nothing writes to the cache by accident.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

ContentKind = Literal["symbol", "prose"]

# Type alias for a provider: given a list of strings + a model name, return
# their embeddings. Kept simple on purpose — the Anthropic / Voyage / OpenAI
# SDKs get thin wrappers that adapt to this shape.
EmbeddingProvider = Callable[[list[str], str], Awaitable[list[list[float]]]]


class EmbeddingCache(Protocol):
    """Shape the pipeline expects of a cache backend."""

    def get(self, key: str) -> list[float] | None: ...

    def put(self, key: str, value: list[float]) -> None: ...


class InMemoryCache:
    """Simple dict-backed cache. Safe for single-process use; not thread-safe."""

    def __init__(self) -> None:
        self._store: dict[str, list[float]] = {}

    def get(self, key: str) -> list[float] | None:
        return self._store.get(key)

    def put(self, key: str, value: list[float]) -> None:
        self._store[key] = value

    def __len__(self) -> int:
        return len(self._store)


@dataclass(slots=True)
class EmbeddingRequest:
    """One item the caller wants embedded."""

    kind: ContentKind
    content: str
    # Caller-supplied correlation tag — echoed back on the result so the
    # caller can route vectors to the right row without hashing twice.
    tag: str = ""


@dataclass(slots=True)
class EmbeddingResult:
    """One vector, plus metadata."""

    tag: str
    kind: ContentKind
    vector: list[float]
    cache_hit: bool


@dataclass(slots=True)
class EmbeddingStats:
    """Running counters the caller can log at the end of an indexing run."""

    cache_hits: int = 0
    cache_misses: int = 0
    api_calls: int = 0
    items_embedded: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)


def cache_key(model: str, content: str) -> str:
    """Return the deterministic cache key for ``(model, content)``.

    SHA-256 of ``model + "\\0" + content`` so two different models never
    collide even if they hash the same string.
    """
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\0")
    h.update(content.encode("utf-8"))
    return h.hexdigest()


class EmbeddingPipeline:
    """Batched, cached fan-out over one or more embedding providers.

    The pipeline is **not** async-context-managed — call :meth:`flush` when
    you're done to drain pending batches. This makes testing easier and
    mirrors how the indexer drives it: one long-lived pipeline per repo run.
    """

    def __init__(
        self,
        *,
        symbol_provider: EmbeddingProvider | None = None,
        prose_provider: EmbeddingProvider | None = None,
        symbol_model: str = "voyage-code-3",
        prose_model: str = "text-embedding-3-large",
        cache: EmbeddingCache | None = None,
        batch_size: int = 100,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self._providers: dict[ContentKind, EmbeddingProvider | None] = {
            "symbol": symbol_provider,
            "prose": prose_provider,
        }
        self._models: dict[ContentKind, str] = {
            "symbol": symbol_model,
            "prose": prose_model,
        }
        self._cache = cache if cache is not None else InMemoryCache()
        self._batch_size = batch_size
        # Pending work keyed by content kind so we can batch with the right
        # provider. Each entry stores (tag, content, cache_key).
        self._pending: dict[ContentKind, list[tuple[str, str, str]]] = {
            "symbol": [],
            "prose": [],
        }
        self._results: list[EmbeddingResult] = []
        self.stats = EmbeddingStats()

    # -- Public API ----------------------------------------------------------

    async def submit(self, request: EmbeddingRequest) -> None:
        """Enqueue ``request``. Flushes automatically once the batch is full."""
        model = self._models[request.kind]
        key = cache_key(model, request.content)
        cached = self._cache.get(key)
        if cached is not None:
            self._results.append(
                EmbeddingResult(
                    tag=request.tag,
                    kind=request.kind,
                    vector=cached,
                    cache_hit=True,
                )
            )
            self.stats.cache_hits += 1
            self.stats.items_embedded += 1
            self.stats.by_kind[request.kind] = self.stats.by_kind.get(request.kind, 0) + 1
            return

        self._pending[request.kind].append((request.tag, request.content, key))
        self.stats.cache_misses += 1
        if len(self._pending[request.kind]) >= self._batch_size:
            await self._flush_kind(request.kind)

    async def flush(self) -> list[EmbeddingResult]:
        """Drain every pending batch; return every result seen so far."""
        for kind in ("symbol", "prose"):
            if self._pending[kind]:
                await self._flush_kind(kind)
        return self._results

    # -- Internals -----------------------------------------------------------

    async def _flush_kind(self, kind: ContentKind) -> None:
        batch = self._pending[kind]
        if not batch:
            return
        provider = self._providers[kind]
        if provider is None:
            raise RuntimeError(
                f"No embedding provider configured for kind={kind!r}; "
                "pass one into EmbeddingPipeline(...) or pre-populate the cache."
            )
        model = self._models[kind]
        contents = [item[1] for item in batch]
        vectors = await provider(contents, model)
        if len(vectors) != len(batch):
            raise RuntimeError(
                f"provider returned {len(vectors)} vectors for {len(batch)} inputs on kind={kind!r}"
            )
        self.stats.api_calls += 1
        for (tag, _content, key), vec in zip(batch, vectors, strict=True):
            self._cache.put(key, vec)
            self._results.append(EmbeddingResult(tag=tag, kind=kind, vector=vec, cache_hit=False))
            self.stats.items_embedded += 1
            self.stats.by_kind[kind] = self.stats.by_kind.get(kind, 0) + 1
        self._pending[kind] = []


__all__ = [
    "ContentKind",
    "EmbeddingCache",
    "EmbeddingPipeline",
    "EmbeddingProvider",
    "EmbeddingRequest",
    "EmbeddingResult",
    "EmbeddingStats",
    "InMemoryCache",
    "cache_key",
]
