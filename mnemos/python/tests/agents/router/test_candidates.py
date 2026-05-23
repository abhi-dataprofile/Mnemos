"""Unit tests for :mod:`codereview.agents.router.candidates`."""

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
from codereview.agents.router.candidates import (
    WINDOW_DAYS,
    assemble_candidates,
    gather_signals,
    is_bot_login,
    load_codeowners_from_workspace,
)
from codereview.agents.router.codeowners import parse_codeowners

# -- Fakes ---------------------------------------------------------------


@dataclass(frozen=True)
class _PersonRef:
    id: UUID
    github_login: str


@dataclass(frozen=True)
class _AuthorshipCount:
    person: _PersonRef
    count: int


@dataclass
class _FakeGraph:
    """Fake GraphClient with just the router's surface implemented."""

    file_ids: dict[str, UUID] = field(default_factory=dict)
    authors_by_file: dict[UUID, list[_AuthorshipCount]] = field(default_factory=dict)
    reviewers_by_file: dict[UUID, list[_AuthorshipCount]] = field(default_factory=dict)
    last_activity: dict[UUID, dt.datetime | None] = field(default_factory=dict)
    call_graph_overlap: dict[UUID, int] = field(default_factory=dict)
    review_rates: dict[UUID, tuple[int, int]] = field(default_factory=dict)
    open_loads: dict[UUID, int] = field(default_factory=dict)

    async def file_by_path(self, _repo_id: UUID, path: str) -> UUID | None:
        return self.file_ids.get(path)

    async def authors_of_file(
        self, file_id: UUID, _since: dt.datetime
    ) -> list[_AuthorshipCount]:
        return self.authors_by_file.get(file_id, [])

    async def reviewers_of_file(
        self, file_id: UUID, _since: dt.datetime
    ) -> list[_AuthorshipCount]:
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


# -- Bot detection --------------------------------------------------------


def test_is_bot_login_matches_standard_patterns() -> None:
    assert is_bot_login("dependabot[bot]")
    assert is_bot_login("some-bot")
    assert is_bot_login("github-actions")
    assert not is_bot_login("alice")
    assert not is_bot_login("bot-carol")  # must be a suffix match
    # ``-actions`` is a suffix — ``actions-user`` is not a bot.
    assert not is_bot_login("actions-user")


# -- assemble_candidates --------------------------------------------------


async def test_assemble_unions_codeowners_authors_and_reviewers() -> None:
    billing_id = uuid4()
    alice = uuid4()
    bob = uuid4()
    carol = uuid4()
    graph = _FakeGraph(
        file_ids={"src/billing/a.py": billing_id},
        authors_by_file={
            billing_id: [
                _AuthorshipCount(_PersonRef(alice, "alice"), 5),
                _AuthorshipCount(_PersonRef(bob, "bob"), 2),
            ]
        },
        reviewers_by_file={
            billing_id: [_AuthorshipCount(_PersonRef(carol, "carol"), 3)]
        },
        last_activity={
            alice: dt.datetime(2026, 4, 10, tzinfo=dt.timezone.utc),
            bob: dt.datetime(2026, 4, 10, tzinfo=dt.timezone.utc),
            carol: dt.datetime(2026, 4, 10, tzinfo=dt.timezone.utc),
        },
    )
    codeowners = parse_codeowners("src/billing/** @dave @acme/billing-team\n")
    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    now = dt.datetime(2026, 4, 15, tzinfo=dt.timezone.utc)
    candidates = await assemble_candidates(ctx, codeowners=codeowners, now=now)
    logins = {c.login for c in candidates}
    # dave from CODEOWNERS, acme/billing-team (team), alice/bob from authors, carol from reviewers.
    assert logins == {"dave", "acme/billing-team", "alice", "bob", "carol"}
    # Team flagged.
    by_login = {c.login: c for c in candidates}
    assert by_login["acme/billing-team"].is_team is True


async def test_assemble_excludes_pr_author() -> None:
    billing_id = uuid4()
    alice = uuid4()
    graph = _FakeGraph(
        file_ids={"src/billing/a.py": billing_id},
        authors_by_file={
            billing_id: [_AuthorshipCount(_PersonRef(alice, "alice"), 5)]
        },
        last_activity={alice: dt.datetime(2026, 4, 10, tzinfo=dt.timezone.utc)},
    )
    pr = _pr(author="alice", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    now = dt.datetime(2026, 4, 15, tzinfo=dt.timezone.utc)
    candidates = await assemble_candidates(ctx, now=now)
    assert candidates == []


async def test_assemble_excludes_bots() -> None:
    billing_id = uuid4()
    bot = uuid4()
    graph = _FakeGraph(
        file_ids={"src/billing/a.py": billing_id},
        authors_by_file={
            billing_id: [_AuthorshipCount(_PersonRef(bot, "dependabot[bot]"), 5)]
        },
        last_activity={bot: dt.datetime(2026, 4, 10, tzinfo=dt.timezone.utc)},
    )
    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    now = dt.datetime(2026, 4, 15, tzinfo=dt.timezone.utc)
    assert await assemble_candidates(ctx, now=now) == []


async def test_assemble_filters_inactive_humans() -> None:
    billing_id = uuid4()
    vacationer = uuid4()
    graph = _FakeGraph(
        file_ids={"src/billing/a.py": billing_id},
        authors_by_file={
            billing_id: [
                _AuthorshipCount(_PersonRef(vacationer, "quiet-quentin"), 5)
            ]
        },
        # Last activity 120 days before "now" — outside the 90-day window.
        last_activity={
            vacationer: dt.datetime(2025, 12, 1, tzinfo=dt.timezone.utc)
        },
    )
    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    now = dt.datetime(2026, 4, 15, tzinfo=dt.timezone.utc)
    assert await assemble_candidates(ctx, now=now) == []


async def test_assemble_keeps_unknown_codeowner_even_without_activity() -> None:
    # CODEOWNERS names @newhire, who has no graph rows yet (no commits,
    # no reviews). We should still surface them — the router is
    # explicitly designed to respect CODEOWNERS even when the graph
    # hasn't caught up.
    graph = _FakeGraph(file_ids={"src/billing/a.py": uuid4()})
    codeowners = parse_codeowners("src/billing/** @newhire\n")
    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    now = dt.datetime(2026, 4, 15, tzinfo=dt.timezone.utc)
    candidates = await assemble_candidates(ctx, codeowners=codeowners, now=now)
    assert [c.login for c in candidates] == ["newhire"]


async def test_assemble_degrades_when_graph_missing_methods() -> None:
    class _Bare:
        async def file_by_path(self, _repo_id: UUID, _path: str) -> UUID | None:
            return None

    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(_Bare(), pr)
    # No authors/reviewers/CODEOWNERS surfaces → empty pool (no crash).
    assert await assemble_candidates(ctx) == []


# -- gather_signals ------------------------------------------------------


async def test_gather_signals_computes_all_six() -> None:
    billing_id = uuid4()
    alice = uuid4()
    graph = _FakeGraph(
        file_ids={"src/billing/a.py": billing_id, "src/billing/b.py": billing_id},
        authors_by_file={
            billing_id: [_AuthorshipCount(_PersonRef(alice, "alice"), 7)]
        },
        reviewers_by_file={
            billing_id: [_AuthorshipCount(_PersonRef(alice, "alice"), 4)]
        },
        last_activity={alice: dt.datetime(2026, 4, 10, tzinfo=dt.timezone.utc)},
        call_graph_overlap={alice: 2},
        review_rates={alice: (8, 10)},
        open_loads={alice: 1},
    )
    from codereview.agents.router.types import Candidate

    cand = Candidate(login="alice", person_id=alice)
    codeowners = parse_codeowners("src/billing/** @alice\n")
    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    now = dt.datetime(2026, 4, 15, tzinfo=dt.timezone.utc)
    sig_map = await gather_signals(ctx, [cand], codeowners=codeowners, now=now)
    s = sig_map["alice"]
    assert s.is_codeowner is True
    # Both b.py and a.py resolve to the same file_id in this fake, so
    # both are considered authored.
    assert s.authorship_share == 1.0
    assert "src/billing/a.py" in s.authored_files
    assert s.recent_review_count == 4
    assert s.call_graph_overlap == 2
    assert s.review_acceptance_rate == 0.8
    assert s.total_reviews == 10
    assert s.open_pr_load == 1


async def test_gather_signals_handles_missing_person_id() -> None:
    # Team CODEOWNERS entry has no person_id — we should still return
    # a :class:`Signals` (with CODEOWNERS flag set, zero everything else).
    billing_id = uuid4()
    graph = _FakeGraph(file_ids={"src/billing/a.py": billing_id})
    from codereview.agents.router.types import Candidate

    cand = Candidate(login="acme/billing", is_team=True)
    codeowners = parse_codeowners("src/billing/** @acme/billing\n")
    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    sig_map = await gather_signals(ctx, [cand], codeowners=codeowners)
    s = sig_map["acme/billing"]
    assert s.is_codeowner is True
    assert s.authorship_share == 0.0
    assert s.recent_review_count == 0
    assert s.review_acceptance_rate == 0.0


async def test_gather_signals_zero_reviews_gives_zero_rate() -> None:
    # Reviewer with no reviews in the window — avoid divide-by-zero.
    billing_id = uuid4()
    alice = uuid4()
    graph = _FakeGraph(
        file_ids={"src/billing/a.py": billing_id},
        review_rates={alice: (0, 0)},
    )
    from codereview.agents.router.types import Candidate

    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(graph, pr)

    sig = (
        await gather_signals(
            ctx, [Candidate(login="alice", person_id=alice)]
        )
    )["alice"]
    assert sig.review_acceptance_rate == 0.0
    assert sig.total_reviews == 0


async def test_gather_signals_tolerates_missing_graph_methods() -> None:
    # Graph only has file_by_path — everything else is missing.
    class _Bare:
        async def file_by_path(self, _repo_id: UUID, _path: str) -> UUID | None:
            return None

    from codereview.agents.router.types import Candidate

    pr = _pr(author="stranger", paths=["src/billing/a.py"])
    ctx = _ctx(_Bare(), pr)
    sig_map = await gather_signals(
        ctx, [Candidate(login="alice", person_id=uuid4())]
    )
    s = sig_map["alice"]
    # All signals at their neutral defaults.
    assert s.authorship_share == 0.0
    assert s.recent_review_count == 0
    assert s.call_graph_overlap == 0
    assert s.review_acceptance_rate == 0.0
    assert s.open_pr_load == 0


# -- load_codeowners_from_workspace ---------------------------------------


def test_load_codeowners_none_without_workspace() -> None:
    assert load_codeowners_from_workspace(None) is None


def test_load_codeowners_reads_github_dir(tmp_path: Path) -> None:
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "CODEOWNERS").write_text("*.py @py-team\n", encoding="utf-8")
    m = load_codeowners_from_workspace(tmp_path)
    assert m is not None
    assert m.owners_for("foo.py") == ("py-team",)


def test_load_codeowners_prefers_github_over_docs(tmp_path: Path) -> None:
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "CODEOWNERS").write_text("*.py @gh-team\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "CODEOWNERS").write_text("*.py @docs-team\n", encoding="utf-8")
    m = load_codeowners_from_workspace(tmp_path)
    assert m is not None
    assert m.owners_for("foo.py") == ("gh-team",)


def test_load_codeowners_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert load_codeowners_from_workspace(tmp_path) is None


# -- Window sanity -------------------------------------------------------


def test_window_matches_six_months_give_or_take() -> None:
    # Keep the magic number documented. 180 days is "6 months" per the
    # plan doc; drift from that deserves a test failure.
    assert WINDOW_DAYS == 180
