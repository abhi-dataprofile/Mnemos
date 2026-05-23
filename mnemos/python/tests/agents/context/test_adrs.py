"""Unit tests for :mod:`codereview.agents.context.adrs`."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from codereview.agents.context.adrs import find_relevant_adrs


@dataclass
class _ADR:
    id: UUID
    title: str
    status: str
    body: str = ""


@dataclass
class _Graph:
    adrs: list[_ADR] = field(default_factory=list)

    async def similar_adrs(self, _embedding: list[float], k: int = 5) -> list[_ADR]:
        return self.adrs[:k]


async def test_returns_empty_when_embedding_none() -> None:
    out = await find_relevant_adrs(pr_embedding=None, graph=_Graph())
    assert out == []


async def test_returns_empty_when_graph_missing_similar_adrs() -> None:
    class _Bare:
        pass

    out = await find_relevant_adrs(pr_embedding=[0.1], graph=_Bare())
    assert out == []


async def test_filters_to_accepted_only() -> None:
    graph = _Graph(
        adrs=[
            _ADR(id=uuid4(), title="ADR-001", status="accepted"),
            _ADR(id=uuid4(), title="ADR-002", status="proposed"),
            _ADR(id=uuid4(), title="ADR-003", status="superseded"),
        ]
    )
    out = await find_relevant_adrs(pr_embedding=[0.1], graph=graph)
    assert [a.title for a in out] == ["ADR-001"]


async def test_preserves_order_from_graph() -> None:
    graph = _Graph(
        adrs=[
            _ADR(id=uuid4(), title=f"ADR-{i}", status="accepted") for i in range(1, 4)
        ]
    )
    out = await find_relevant_adrs(pr_embedding=[0.1], graph=graph)
    assert [a.title for a in out] == ["ADR-1", "ADR-2", "ADR-3"]


async def test_swallows_graph_errors() -> None:
    class _Raising:
        async def similar_adrs(self, _embedding: list[float], k: int = 5) -> list[_ADR]:
            raise RuntimeError("nope")

    out = await find_relevant_adrs(pr_embedding=[0.1], graph=_Raising())
    assert out == []


async def test_skips_adrs_with_empty_title() -> None:
    graph = _Graph(
        adrs=[
            _ADR(id=uuid4(), title="", status="accepted"),
            _ADR(id=uuid4(), title="ADR-good", status="accepted"),
        ]
    )
    out = await find_relevant_adrs(pr_embedding=[0.1], graph=graph)
    assert [a.title for a in out] == ["ADR-good"]
