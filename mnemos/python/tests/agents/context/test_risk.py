"""Unit tests for :mod:`codereview.agents.context.risk`."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from codereview.agents.base import ChangedFile
from codereview.agents.context.risk import compute_risk_notes

_NOW = dt.datetime(2026, 4, 19, tzinfo=dt.timezone.utc)


@dataclass
class _Commit:
    sha: str


@dataclass
class _FakeGraph:
    file_ids: dict[str, UUID] = field(default_factory=dict)
    reverts: dict[UUID, list[_Commit]] = field(default_factory=dict)
    churn: dict[UUID, int] = field(default_factory=dict)
    file_by_path_error: bool = False

    async def file_by_path(self, _repo_id: UUID, path: str) -> UUID | None:
        if self.file_by_path_error:
            raise RuntimeError("boom")
        return self.file_ids.get(path)

    async def revert_commits_touching(
        self, file_id: UUID, _since: dt.datetime
    ) -> list[_Commit]:
        return self.reverts.get(file_id, [])

    async def commit_count_for_file_since(
        self, file_id: UUID, _since: dt.datetime
    ) -> int:
        return self.churn.get(file_id, 0)


def _cf(path: str, patch: str = "") -> ChangedFile:
    return ChangedFile(path=path, change_kind="modified", patch=patch)


# -- Large-PR heuristic ---------------------------------------------------


async def test_large_pr_heuristic_counts_plus_and_minus_lines() -> None:
    patch = "\n".join(
        ["--- a/x.py", "+++ b/x.py", "@@ hunk @@"]
        + ["+added"] * 300
        + ["-removed"] * 300
    )
    out = await compute_risk_notes(
        repo_id=uuid4(),
        changed_files=[_cf("x.py", patch)],
        graph=_FakeGraph(),
        now=_NOW,
    )
    assert any("Large PR" in n for n in out)
    assert any("600 lines" in n for n in out)


async def test_large_pr_heuristic_skips_file_headers() -> None:
    """``+++`` / ``---`` header lines must not inflate the line count."""
    patch = "\n".join(["--- a/x.py", "+++ b/x.py"] * 400)
    out = await compute_risk_notes(
        repo_id=uuid4(),
        changed_files=[_cf("x.py", patch)],
        graph=_FakeGraph(),
        now=_NOW,
    )
    assert out == []


async def test_small_pr_produces_no_large_note() -> None:
    patch = "+one\n-two\n"
    out = await compute_risk_notes(
        repo_id=uuid4(),
        changed_files=[_cf("x.py", patch)],
        graph=_FakeGraph(),
        now=_NOW,
    )
    assert out == []


async def test_large_pr_threshold_is_configurable() -> None:
    patch = "\n".join(["+x"] * 6)
    out = await compute_risk_notes(
        repo_id=uuid4(),
        changed_files=[_cf("x.py", patch)],
        graph=_FakeGraph(),
        now=_NOW,
        large_pr_threshold=5,
    )
    assert any("Large PR" in n for n in out)


# -- Revert heuristic -----------------------------------------------------


async def test_revert_heuristic_fires_when_commits_present() -> None:
    fid = uuid4()
    graph = _FakeGraph(
        file_ids={"x.py": fid},
        reverts={fid: [_Commit(sha="abcdef1234567890")]},
    )
    out = await compute_risk_notes(
        repo_id=uuid4(),
        changed_files=[_cf("x.py")],
        graph=graph,
        now=_NOW,
    )
    assert any("Recently reverted" in n for n in out)
    assert any("abcdef1" in n for n in out)


async def test_revert_heuristic_silent_when_none() -> None:
    fid = uuid4()
    graph = _FakeGraph(file_ids={"x.py": fid}, reverts={fid: []})
    out = await compute_risk_notes(
        repo_id=uuid4(),
        changed_files=[_cf("x.py")],
        graph=graph,
        now=_NOW,
    )
    assert out == []


# -- Churn heuristic ------------------------------------------------------


async def test_churn_heuristic_fires_above_threshold() -> None:
    fid = uuid4()
    graph = _FakeGraph(file_ids={"x.py": fid}, churn={fid: 25})
    out = await compute_risk_notes(
        repo_id=uuid4(),
        changed_files=[_cf("x.py")],
        graph=graph,
        now=_NOW,
        churn_threshold=20,
    )
    assert any("High churn" in n and "25 commits" in n for n in out)


async def test_churn_heuristic_silent_at_or_below_threshold() -> None:
    fid = uuid4()
    graph = _FakeGraph(file_ids={"x.py": fid}, churn={fid: 20})
    out = await compute_risk_notes(
        repo_id=uuid4(),
        changed_files=[_cf("x.py")],
        graph=graph,
        now=_NOW,
        churn_threshold=20,
    )
    assert out == []


# -- Degradation ----------------------------------------------------------


async def test_empty_changed_files_returns_empty() -> None:
    out = await compute_risk_notes(
        repo_id=uuid4(),
        changed_files=[],
        graph=_FakeGraph(),
        now=_NOW,
    )
    assert out == []


async def test_graph_missing_file_by_path_skips_per_file_checks() -> None:
    class _Bare:
        pass

    patch = "\n".join(["+x"] * 600)
    out = await compute_risk_notes(
        repo_id=uuid4(),
        changed_files=[_cf("x.py", patch)],
        graph=_Bare(),
        now=_NOW,
    )
    # Large-PR note still fires because it is pure arithmetic.
    assert any("Large PR" in n for n in out)


async def test_file_by_path_error_skips_that_file() -> None:
    graph = _FakeGraph(file_by_path_error=True)
    out = await compute_risk_notes(
        repo_id=uuid4(),
        changed_files=[_cf("x.py")],
        graph=graph,
        now=_NOW,
    )
    assert out == []


async def test_revert_error_does_not_block_churn_note() -> None:
    """One sub-check failing shouldn't kill the other."""

    fid = uuid4()

    class _Mixed:
        async def file_by_path(self, _repo_id: UUID, _path: str) -> UUID:
            return fid

        async def revert_commits_touching(self, _fid: UUID, _since: dt.datetime) -> Any:
            raise RuntimeError("revert table down")

        async def commit_count_for_file_since(
            self, _fid: UUID, _since: dt.datetime
        ) -> int:
            return 50

    out = await compute_risk_notes(
        repo_id=uuid4(),
        changed_files=[_cf("x.py")],
        graph=_Mixed(),
        now=_NOW,
    )
    assert any("High churn" in n for n in out)


async def test_since_window_respects_window_days() -> None:
    """``since`` should be ``now - window_days``; capture via a spy graph."""

    captured: dict[str, dt.datetime] = {}
    fid = uuid4()

    class _SpyGraph:
        async def file_by_path(self, _repo_id: UUID, _path: str) -> UUID:
            return fid

        async def revert_commits_touching(
            self, _fid: UUID, since: dt.datetime
        ) -> list[_Commit]:
            captured["since"] = since
            return []

        async def commit_count_for_file_since(
            self, _fid: UUID, _since: dt.datetime
        ) -> int:
            return 0

    await compute_risk_notes(
        repo_id=uuid4(),
        changed_files=[_cf("x.py")],
        graph=_SpyGraph(),
        now=_NOW,
        window_days=7,
    )
    assert captured["since"] == _NOW - dt.timedelta(days=7)
