"""Unit tests for :mod:`codereview.llm.client` — the prose embedder surface.

The ``structured_call`` path is exercised via end-to-end / contract tests; this
module covers the Phase 5 prose embedder extension in isolation so the
Context Packager can rely on it without booting an Anthropic client.
"""

from __future__ import annotations

from typing import Any

import pytest

from codereview.llm.client import LLMClient, LLMClientError


class _FakeAnthropic:
    """Stand-in for AsyncAnthropic — we never make network calls in these tests."""

    class _Messages:
        async def create(self, **_kwargs: Any) -> Any:  # pragma: no cover - unused
            raise AssertionError("structured_call should not fire in embed tests")

    def __init__(self) -> None:
        self.messages = self._Messages()


def _client(**kwargs: Any) -> LLMClient:
    # Pass a fake AsyncAnthropic to bypass real SDK construction.
    fake = _FakeAnthropic()
    return LLMClient(client=fake, **kwargs)  # type: ignore[arg-type]


# -- Configuration guard --------------------------------------------------


async def test_embed_prose_raises_without_embedder_configured() -> None:
    client = _client()
    with pytest.raises(LLMClientError, match="no prose embedder"):
        await client.embed_prose("hello")


# -- Happy path -----------------------------------------------------------


async def test_embed_prose_dispatches_to_provider_with_configured_model() -> None:
    captured: dict[str, Any] = {}

    async def fake_embedder(texts: list[str], model: str) -> list[list[float]]:
        captured["texts"] = list(texts)
        captured["model"] = model
        return [[0.1, 0.2, 0.3]]

    client = _client(
        prose_embedder=fake_embedder,
        prose_embedding_model="text-embedding-3-fake",
    )
    vector = await client.embed_prose("review this PR")

    assert vector == [0.1, 0.2, 0.3]
    assert captured["texts"] == ["review this PR"]
    assert captured["model"] == "text-embedding-3-fake"


async def test_embed_prose_uses_default_model_when_unset() -> None:
    captured: dict[str, Any] = {}

    async def fake_embedder(texts: list[str], model: str) -> list[list[float]]:
        captured["model"] = model
        return [[0.0]]

    client = _client(prose_embedder=fake_embedder)
    await client.embed_prose("x")
    assert captured["model"] == "text-embedding-3-large"


# -- Empty-response guard -------------------------------------------------


async def test_embed_prose_raises_when_provider_returns_empty() -> None:
    async def empty_embedder(_texts: list[str], _model: str) -> list[list[float]]:
        return []

    client = _client(prose_embedder=empty_embedder)
    with pytest.raises(LLMClientError, match="no vectors"):
        await client.embed_prose("x")


async def test_embed_prose_returns_new_list_not_provider_internal() -> None:
    """The client should not hand back the provider's internal list by reference."""

    internal = [0.5, 0.25]

    async def returns_internal(_texts: list[str], _model: str) -> list[list[float]]:
        return [internal]

    client = _client(prose_embedder=returns_internal)
    vector = await client.embed_prose("x")
    assert vector == internal
    assert vector is not internal
