"""End-to-end tests for :class:`codereview.agents.router.router.ReviewerRouter`.

These tests exercise the full agent (candidate assembly → signal
gathering → scoring → top-k → finding/metadata shaping) against a fake
GraphClient. They are intentionally behaviour-level: we assert on the
*rank order* and the *finding shape* rather than on intermediate
internals.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from codereview.agents.base import (
    AgentContext,
    ChangedFile,
    PullRequestSnapshot,
)
from codereview.agents.router.router import ReviewerRouter

# -- Fakes ---------------------------------------------------------------


@dataclass(frozen=True)
class _PersonRef:
    id: UUID
    github_login: str


@dataclass(frozen=True)
class _Count:
    person: _PersonRef
    count: int


@dataclass
class _FakeGraph:
    """Graph-shaped fake for the router. Same surface as candidates.py tests."""

    file_ids: dict[str, UUID] = field(default_factory=dict)
    authors_by_file: dict[UUID, list[_Count]] = field(default_factory=dict)
    reviewers_by_file: dict[UUID, list[_Count]] = field(default_factory=dict)
    last_activity: dict[UUID, dt.datetime | None] = field(default_factory=dict)
    call_graph_overlap: dict[UUID, int] = field(default_factory=dict)
    review_rates: dict[UUID, tuple[int, int]] = field(default_factory=dict)
    open_loads: dict[UUID, int] = field(default_factory=dict)

    async def file_by_path(self, _repo_id: UUID, path: str) -> UUID | None:
        return self.file_ids.get(path)

    async def authors_of_file(
        self, file_id: UUID, _since: dt.datetime
    ) -> list[_Count]:
        return self.authors_by_file.get(file_id, [])

    async def reviewers_of_file(
        self, file_id: UUID, _since: dt.datetime
    ) -> list[_Count]:
        return self.reviewers_by_file.get(file_id, [])

    async def last_activity_at(self, person_id: UUID) -> dt.datetime | None:
        return self.last_activity.get(person_id)

    async def call_graph_overlap_counts(
        self, _file_ids: list[UUID], _since: dt.datetime
    ) -> dict[UUID, int]:
        return dict(self.call_graph_overlap)

    async def review_acceptance_rate(
        self, person_id: UUID, _since: dt.datetime
    ) -> tuple[int, int]:
        return self.review_rates.get(person_id, (0, 0))

    async def open_prs_assigned_to(self, person_id: UUID) -> int:
        return self.open_loads.get(person_id, 0)


def _pr(author: str, paths: list[str]) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        number=42,
        title="change things",
        body="",
        author=author,
        head_sha="h" * 40,
        base_sha="b" * 40,
        changed_files=[
            ChangedFile(path=p, change_kind="modified") for p in paths
        ],
    )


def _ctx(graph: Any, pr: PullRequestSnapshot) -> AgentContext:
    return AgentContext(
        pr=pr,
        repo_id=uuid4(),
        graph=graph,
        llm=None,
        config={},
    )


def _recent() -> dt.datetime:
    # Comfortably inside the 90-day activity window for today (2026-04-19).
    return dt.datetime(2026, 4, 10, tzinfo=dt.timezone.utc)


# -- Happy path -----------------------------------------------------------


async def test_router_ranks_strong_signals_first() -> None:
    # Alice: CODEOWNERS + authored → expect rank 1.
    # Bob: authored only.
    # Carol: reviewer only with high acceptance.
    # Expect alice > bob > carol by score; all three in top-3.
    billing_id = uuid4()
    alice, bob, carol = uuid4(), uuid4(), uuid4()
    graph = _FakeGraph(
        file_ids={"src/billing/a.py": billing_id},
        authors_by_file={
            billing_id: [
                _Count(_PersonRef(alice, "alice"), 10),
                _Count(_PersonRef(bob, "bob"), 3),
            ],
        },
        reviewers_by_file={
            billing_id: [_Count(_PersonRef(carol, "carol"), 5)],
        },
        last_activity={
            alice: _recent(), bob: _recent(), carol: _recent(),
        },
        review_rates={carol: (9, 10)},
        open_loads={alice: 1, bob: 1, carol: 1},
    )
    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    result = await ReviewerRouter().run(ctx)

    # Findings are present and look like reviewer suggestions.
    assert len(result.findings) == 3
    for f in result.findings:
        assert f.severity == "info"
        assert f.kind == "reviewer_suggestion"
        assert f.title.startswith("Suggested reviewer #")

    # Metadata carries the ranked list in order.
    ranked = result.metadata["suggested_reviewers"]
    logins = [entry["login"] for entry in ranked]
    assert logins[0] == "alice"
    assert set(logins) == {"alice", "bob", "carol"}
    # Scores are monotonically non-increasing.
    scores = [entry["score"] for entry in ranked]
    assert scores == sorted(scores, reverse=True)


async def test_router_load_penalty_demotes_senior_below_mid() -> None:
    # Senior alice is the top author but is swamped with 12 open PRs.
    # Mid bob is also an author with capacity. Despite matching the same
    # ``authored`` signal, bob should end up ranked ahead of alice
    # because the −0.20 load penalty + loss of the capacity bonus pushes
    # alice below bob.
    billing_id = uuid4()
    alice, bob = uuid4(), uuid4()
    graph = _FakeGraph(
        file_ids={"src/billing/a.py": billing_id},
        authors_by_file={
            billing_id: [
                _Count(_PersonRef(alice, "alice"), 20),
                _Count(_PersonRef(bob, "bob"), 5),
            ],
        },
        last_activity={alice: _recent(), bob: _recent()},
        open_loads={alice: 12, bob: 1},
    )
    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    result = await ReviewerRouter().run(ctx)
    ranked = result.metadata["suggested_reviewers"]
    # Both are present.
    logins = [e["login"] for e in ranked]
    assert "alice" in logins
    assert "bob" in logins
    # Alice: authorship(+0.25) + penalty(-0.20) = 0.05
    # Bob:   authorship(+0.25) + capacity(+0.10) = 0.35
    # So bob ranks strictly above alice.
    assert logins.index("bob") < logins.index("alice")
    alice_score = next(e["score"] for e in ranked if e["login"] == "alice")
    bob_score = next(e["score"] for e in ranked if e["login"] == "bob")
    assert alice_score < bob_score


async def test_router_excludes_pr_author_from_suggestions() -> None:
    billing_id = uuid4()
    alice, bob = uuid4(), uuid4()
    graph = _FakeGraph(
        file_ids={"src/billing/a.py": billing_id},
        authors_by_file={
            billing_id: [
                _Count(_PersonRef(alice, "alice"), 10),
                _Count(_PersonRef(bob, "bob"), 3),
            ],
        },
        last_activity={alice: _recent(), bob: _recent()},
        open_loads={alice: 1, bob: 1},
    )
    pr = _pr(author="alice", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    result = await ReviewerRouter().run(ctx)
    ranked = result.metadata["suggested_reviewers"]
    assert all(e["login"] != "alice" for e in ranked)
    # Bob is the only remaining author, so he's the only suggestion.
    assert [e["login"] for e in ranked] == ["bob"]


async def test_router_filters_bots_transparently() -> None:
    billing_id = uuid4()
    alice, bot = uuid4(), uuid4()
    graph = _FakeGraph(
        file_ids={"src/billing/a.py": billing_id},
        authors_by_file={
            billing_id: [
                _Count(_PersonRef(alice, "alice"), 10),
                _Count(_PersonRef(bot, "dependabot[bot]"), 30),
            ],
        },
        last_activity={alice: _recent(), bot: _recent()},
        open_loads={alice: 1, bot: 1},
    )
    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    result = await ReviewerRouter().run(ctx)
    logins = [e["login"] for e in result.metadata["suggested_reviewers"]]
    assert "dependabot[bot]" not in logins
    assert "alice" in logins


async def test_router_top_k_caps_at_three() -> None:
    billing_id = uuid4()
    graph = _FakeGraph(file_ids={"src/billing/a.py": billing_id})
    authors = [(uuid4(), f"dev{i}") for i in range(6)]
    graph.authors_by_file[billing_id] = [
        _Count(_PersonRef(pid, login), 5) for pid, login in authors
    ]
    graph.last_activity = {pid: _recent() for pid, _ in authors}
    graph.open_loads = {pid: 1 for pid, _ in authors}
    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    result = await ReviewerRouter().run(ctx)
    assert len(result.findings) == 3
    assert len(result.metadata["suggested_reviewers"]) == 3


# -- Edge paths ----------------------------------------------------------


async def test_router_returns_empty_when_no_candidates() -> None:
    # Graph has nothing to offer; expect empty findings and empty
    # metadata list (not missing).
    graph = _FakeGraph()
    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    result = await ReviewerRouter().run(ctx)
    assert result.findings == []
    # metadata.sections is populated; suggested_reviewers is not
    # written when there are no candidates.
    assert "suggested_reviewers" not in result.metadata
    assert result.metadata["sections"]["candidates"]["count"] == 0


async def test_router_degrades_gracefully_on_signals_exception() -> None:
    # A graph whose signal method raises — simulate a transient DB
    # failure in gather_signals. We expect the router to return no
    # suggestions rather than crash the coordinator.
    billing_id = uuid4()
    alice = uuid4()

    class _BoomGraph(_FakeGraph):
        async def call_graph_overlap_counts(
            self, _fids: list[UUID], _since: dt.datetime
        ) -> dict[UUID, int]:
            raise RuntimeError("db is having a moment")

    graph = _BoomGraph(
        file_ids={"src/billing/a.py": billing_id},
        authors_by_file={
            billing_id: [_Count(_PersonRef(alice, "alice"), 10)]
        },
        last_activity={alice: _recent()},
        open_loads={alice: 1},
    )
    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    # The router wraps call_graph_overlap_counts in a _safe_* helper, so
    # this particular failure degrades to zero overlap rather than
    # escaping. To exercise the outer try/except, patch gather_signals
    # itself via a monkeypatched method.
    from codereview.agents.router import router as router_mod

    async def _boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    orig = router_mod.gather_signals
    router_mod.gather_signals = _boom  # type: ignore[assignment]
    try:
        result = await ReviewerRouter().run(ctx)
    finally:
        router_mod.gather_signals = orig  # type: ignore[assignment]

    assert result.findings == []
    assert "error" in result.metadata["sections"]["signals"]


async def test_router_drops_zero_scoring_candidates() -> None:
    # A candidate whose only signal was "surfaced as a reviewer" with no
    # capacity bonus (load at threshold) and no other matching signals
    # will score 0.0 and should not appear in suggestions.
    billing_id = uuid4()
    quiet = uuid4()
    graph = _FakeGraph(
        file_ids={"src/billing/a.py": billing_id},
        reviewers_by_file={
            billing_id: [_Count(_PersonRef(quiet, "quiet-quentin"), 1)]
        },
        last_activity={quiet: _recent()},
        # Load at threshold → no capacity bonus; nothing else fires.
        open_loads={quiet: 3},
    )
    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    result = await ReviewerRouter().run(ctx)
    logins = [e["login"] for e in result.metadata.get("suggested_reviewers", [])]
    assert "quiet-quentin" not in logins


async def test_router_marks_teams_with_is_team_flag(tmp_path: Path) -> None:
    # Team CODEOWNERS entry should come through with ``is_team: True``
    # on its metadata payload. Load via workspace_root so the full
    # agent code path is exercised.
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "CODEOWNERS").write_text(
        "src/billing/** @acme/billing-team\n", encoding="utf-8"
    )
    billing_id = uuid4()
    graph = _FakeGraph(file_ids={"src/billing/a.py": billing_id})
    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = AgentContext(
        pr=pr,
        repo_id=uuid4(),
        graph=graph,
        llm=None,
        config={},
        workspace_root=tmp_path,
    )

    result = await ReviewerRouter().run(ctx)
    ranked = result.metadata["suggested_reviewers"]
    assert any(e["login"] == "acme/billing-team" for e in ranked)
    team_entry = next(e for e in ranked if e["login"] == "acme/billing-team")
    assert team_entry.get("is_team") is True
    # Codeowners metadata reflects the successful load.
    assert result.metadata["sections"]["codeowners"]["found"] is True
    assert result.metadata["sections"]["codeowners"]["entries"] >= 1


async def test_router_metadata_records_codeowners_absence() -> None:
    billing_id = uuid4()
    graph = _FakeGraph(file_ids={"src/billing/a.py": billing_id})
    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    # No workspace_root → load_codeowners_from_workspace returns None.
    result = await ReviewerRouter().run(ctx)
    assert result.metadata["sections"]["codeowners"]["found"] is False
    assert result.metadata["sections"]["codeowners"]["entries"] == 0
