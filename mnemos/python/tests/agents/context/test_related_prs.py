"""Unit tests for :mod:`codereview.agents.context.related_prs`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from codereview.agents.context.related_prs import (
    find_related_prs,
    jaccard,
)

# -- Jaccard --------------------------------------------------------------


def test_jaccard_identical_sets_is_one() -> None:
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0


def test_jaccard_disjoint_sets_is_zero() -> None:
    assert jaccard({"a"}, {"b"}) == 0.0


def test_jaccard_empty_inputs_is_zero() -> None:
    assert jaccard(set(), set()) == 0.0


def test_jaccard_partial_overlap() -> None:
    assert jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)


# -- Fake graph ------------------------------------------------------------


@dataclass
class _FakePRRef:
    id: UUID
    number: int
    title: str
    body: str = ""
    author_login: str | None = None
    merged_at: Any = None


@dataclass
class _FakePRGraph:
    """Test double for :class:`GraphClient` (related-PR surface only)."""

    scored: list[tuple[_FakePRRef, float]] = field(default_factory=list)
    files_by_pr: dict[UUID, list[str]] = field(default_factory=dict)

    async def similar_prs_scored(
        self, _embedding: list[float], k: int = 5
    ) -> list[tuple[_FakePRRef, float]]:
        return self.scored[:k]

    async def files_touched_by_pr(self, pr_id: UUID) -> list[str]:
        return self.files_by_pr.get(pr_id, [])


def _pr(*, number: int, title: str, author: str | None = None) -> _FakePRRef:
    return _FakePRRef(id=uuid4(), number=number, title=title, author_login=author)


# -- find_related_prs ------------------------------------------------------


async def test_returns_empty_when_embedding_is_none() -> None:
    graph = _FakePRGraph()
    out = await find_related_prs(
        pr_embedding=None,
        pr_files={"a.py"},
        pr_author="abhi",
        pr_number=99,
        graph=graph,
    )
    assert out == []


async def test_excludes_pr_itself() -> None:
    self_pr = _pr(number=99, title="self")
    graph = _FakePRGraph(
        scored=[(self_pr, 0.95)],
        files_by_pr={self_pr.id: ["a.py"]},
    )
    out = await find_related_prs(
        pr_embedding=[0.1],
        pr_files={"a.py"},
        pr_author="abhi",
        pr_number=99,
        graph=graph,
    )
    assert out == []


async def test_excludes_own_prior_prs_below_overlap_threshold() -> None:
    mine = _pr(number=10, title="my old PR", author="abhi")
    theirs = _pr(number=11, title="somebody else", author="bob")
    graph = _FakePRGraph(
        scored=[(mine, 0.9), (theirs, 0.9)],
        files_by_pr={
            mine.id: ["unrelated.py"],  # low overlap
            theirs.id: ["a.py"],
        },
    )
    out = await find_related_prs(
        pr_embedding=[0.1],
        pr_files={"a.py"},
        pr_author="abhi",
        pr_number=99,
        graph=graph,
    )
    numbers = [pr.number for pr in out]
    assert 10 not in numbers
    assert 11 in numbers


async def test_own_pr_kept_when_overlap_exceeds_threshold() -> None:
    """File-overlap override: author's own PR on the same files IS relevant."""

    mine = _pr(number=10, title="my old PR", author="abhi")
    graph = _FakePRGraph(
        scored=[(mine, 0.9)],
        files_by_pr={mine.id: ["a.py", "b.py", "c.py"]},
    )
    out = await find_related_prs(
        pr_embedding=[0.1],
        pr_files={"a.py", "b.py", "c.py"},
        pr_author="abhi",
        pr_number=99,
        graph=graph,
    )
    assert [pr.number for pr in out] == [10]


async def test_blended_score_orders_by_similarity_plus_jaccard() -> None:
    a = _pr(number=1, title="a")
    b = _pr(number=2, title="b")
    c = _pr(number=3, title="c")
    graph = _FakePRGraph(
        scored=[(a, 0.9), (b, 0.6), (c, 0.4)],
        files_by_pr={
            a.id: ["unrelated.py"],           # 0.9 * 0.6 + 0 * 0.4 = 0.54
            b.id: ["x.py", "y.py"],           # 0.6 * 0.6 + 1.0 * 0.4 = 0.76
            c.id: ["x.py"],                   # 0.4 * 0.6 + 0.5 * 0.4 = 0.44
        },
    )
    out = await find_related_prs(
        pr_embedding=[0.1],
        pr_files={"x.py", "y.py"},
        pr_author="abhi",
        pr_number=99,
        graph=graph,
        k=3,
    )
    assert [pr.number for pr in out] == [2, 1, 3]


async def test_filters_below_min_score_floor() -> None:
    low = _pr(number=1, title="low")
    graph = _FakePRGraph(
        scored=[(low, 0.1)],  # 0.1 * 0.6 + 0 * 0.4 = 0.06 → below floor
        files_by_pr={low.id: ["nothing.py"]},
    )
    out = await find_related_prs(
        pr_embedding=[0.1],
        pr_files={"a.py"},
        pr_author="abhi",
        pr_number=99,
        graph=graph,
    )
    assert out == []


async def test_trims_to_k() -> None:
    prs = [_pr(number=i, title=f"pr{i}") for i in range(1, 6)]
    graph = _FakePRGraph(
        scored=[(p, 0.8) for p in prs],
        files_by_pr={p.id: ["a.py"] for p in prs},
    )
    out = await find_related_prs(
        pr_embedding=[0.1],
        pr_files={"a.py"},
        pr_author="abhi",
        pr_number=99,
        graph=graph,
        k=2,
    )
    assert len(out) == 2


async def test_returns_empty_when_graph_missing_methods() -> None:
    """Duck-typing: a graph without the required methods should degrade, not raise."""

    class _Bare:
        pass

    out = await find_related_prs(
        pr_embedding=[0.1],
        pr_files={"a.py"},
        pr_author="abhi",
        pr_number=99,
        graph=_Bare(),
    )
    assert out == []


async def test_similarity_failure_returns_empty_without_raising() -> None:
    class _RaisingGraph:
        async def similar_prs_scored(self, _embedding: list[float], k: int = 5) -> Any:
            raise RuntimeError("pgvector exploded")

        async def files_touched_by_pr(self, _pr_id: UUID) -> list[str]:
            return []

    out = await find_related_prs(
        pr_embedding=[0.1],
        pr_files={"a.py"},
        pr_author="abhi",
        pr_number=99,
        graph=_RaisingGraph(),
    )
    assert out == []
