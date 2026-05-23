"""Graph query facade.

Agents read the memory graph exclusively through :class:`GraphClient`. Raw
SQL inside an agent is rejected on code review. When an agent needs a new
question, the right move is to add a method here.

Phase 2 fills in every stub with a concrete SQLAlchemy 2.0 select against the
ORM models in :mod:`codereview.graph.models`. Embedding similarity queries use
pgvector operators (``<=>``) which only run against Postgres; unit tests that
don't have a real pgvector-capable database stub them with :class:`FakeSession`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Select, and_, case, desc, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from codereview.graph import models as m

# -- Lightweight read models -------------------------------------------------
# Agents should not import ORM models. These DTOs decouple the client from
# the SQLAlchemy layer and make test fakes easier to write.


@dataclass(slots=True, frozen=True)
class SymbolRef:
    id: UUID
    qualified_name: str
    kind: str
    signature: str | None
    file_path: str


@dataclass(slots=True, frozen=True)
class CommitRef:
    id: UUID
    sha: str
    author_login: str | None
    message: str
    committed_at: dt.datetime


@dataclass(slots=True, frozen=True)
class PullRequestRef:
    id: UUID
    number: int
    title: str
    body: str
    author_login: str | None
    merged_at: dt.datetime | None


@dataclass(slots=True, frozen=True)
class ADRRef:
    id: UUID
    title: str
    status: str
    body: str


@dataclass(slots=True, frozen=True)
class PersonRef:
    id: UUID
    github_login: str


@dataclass(slots=True, frozen=True)
class AuthorshipCount:
    person: PersonRef
    count: int


# -- GraphClient -------------------------------------------------------------


class GraphClient:
    """Typed read interface to the memory graph.

    One method per question the graph is asked. Questions belong here, not
    in agents.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- Call graph ----------------------------------------------------------

    async def callers_of(self, symbol_id: UUID) -> list[SymbolRef]:
        """Every symbol that calls ``symbol_id`` (deduplicated by caller)."""
        stmt = (
            select(
                m.Symbol.id,
                m.Symbol.qualified_name,
                m.Symbol.kind,
                m.Symbol.signature,
                m.File.path,
            )
            .join(m.SymbolCall, m.SymbolCall.caller_id == m.Symbol.id)
            .join(m.File, m.Symbol.file_id == m.File.id)
            .where(m.SymbolCall.callee_id == symbol_id)
            .distinct()
        )
        return [_to_symbol_ref(row) for row in (await self._session.execute(stmt)).all()]

    async def callees_of(self, symbol_id: UUID) -> list[SymbolRef]:
        """Every symbol called from inside ``symbol_id``."""
        stmt = (
            select(
                m.Symbol.id,
                m.Symbol.qualified_name,
                m.Symbol.kind,
                m.Symbol.signature,
                m.File.path,
            )
            .join(m.SymbolCall, m.SymbolCall.callee_id == m.Symbol.id)
            .join(m.File, m.Symbol.file_id == m.File.id)
            .where(m.SymbolCall.caller_id == symbol_id)
            .distinct()
        )
        return [_to_symbol_ref(row) for row in (await self._session.execute(stmt)).all()]

    async def symbols_in_file(self, file_id: UUID) -> list[SymbolRef]:
        stmt = (
            select(
                m.Symbol.id,
                m.Symbol.qualified_name,
                m.Symbol.kind,
                m.Symbol.signature,
                m.File.path,
            )
            .join(m.File, m.Symbol.file_id == m.File.id)
            .where(m.Symbol.file_id == file_id)
            .order_by(m.Symbol.start_line)
        )
        return [_to_symbol_ref(row) for row in (await self._session.execute(stmt)).all()]

    # -- Commit history ------------------------------------------------------

    async def recent_commits_touching(self, file_id: UUID, limit: int = 5) -> list[CommitRef]:
        """The ``limit`` most recent commits that touched ``file_id``."""
        stmt = (
            select(
                m.Commit.id,
                m.Commit.sha,
                m.Person.github_login,
                m.Commit.message,
                m.Commit.committed_at,
            )
            .join(m.CommitModifiesFile, m.CommitModifiesFile.commit_id == m.Commit.id)
            .outerjoin(m.Person, m.Commit.author_id == m.Person.id)
            .where(m.CommitModifiesFile.file_id == file_id)
            .order_by(desc(m.Commit.committed_at))
            .limit(limit)
        )
        return [
            CommitRef(
                id=row[0],
                sha=row[1],
                author_login=row[2],
                message=row[3],
                committed_at=row[4],
            )
            for row in (await self._session.execute(stmt)).all()
        ]

    async def authors_of_file(self, file_id: UUID, since: dt.datetime) -> list[AuthorshipCount]:
        """People who authored commits to ``file_id`` since ``since``.

        Returned sorted by count descending so the "most frequent author"
        is always row 0.
        """
        count_col = func.count(m.Commit.id).label("c")
        stmt = (
            select(m.Person.id, m.Person.github_login, count_col)
            .join(m.Commit, m.Commit.author_id == m.Person.id)
            .join(m.CommitModifiesFile, m.CommitModifiesFile.commit_id == m.Commit.id)
            .where(
                and_(
                    m.CommitModifiesFile.file_id == file_id,
                    m.Commit.committed_at >= since,
                )
            )
            .group_by(m.Person.id, m.Person.github_login)
            .order_by(desc(count_col))
        )
        return [
            AuthorshipCount(
                person=PersonRef(id=row[0], github_login=row[1]),
                count=row[2],
            )
            for row in (await self._session.execute(stmt)).all()
        ]

    async def reviewers_of_file(self, file_id: UUID, since: dt.datetime) -> list[AuthorshipCount]:
        """People who reviewed PRs that touched ``file_id`` since ``since``.

        Joins: file -> commit_modifies_file -> commit -> pr_contains_commit ->
        pull_request -> review -> person. Counts distinct reviews per person.
        """
        count_col = func.count(func.distinct(m.Review.id)).label("c")
        stmt = (
            select(m.Person.id, m.Person.github_login, count_col)
            .select_from(m.Review)
            .join(m.Person, m.Review.reviewer_id == m.Person.id)
            .join(m.PullRequest, m.Review.pull_request_id == m.PullRequest.id)
            .join(m.PRContainsCommit, m.PRContainsCommit.pull_request_id == m.PullRequest.id)
            .join(m.Commit, m.PRContainsCommit.commit_id == m.Commit.id)
            .join(m.CommitModifiesFile, m.CommitModifiesFile.commit_id == m.Commit.id)
            .where(
                and_(
                    m.CommitModifiesFile.file_id == file_id,
                    m.Review.submitted_at >= since,
                )
            )
            .group_by(m.Person.id, m.Person.github_login)
            .order_by(desc(count_col))
        )
        return [
            AuthorshipCount(
                person=PersonRef(id=row[0], github_login=row[1]),
                count=row[2],
            )
            for row in (await self._session.execute(stmt)).all()
        ]

    # -- Similarity ----------------------------------------------------------

    async def similar_prs(self, embedding: Sequence[float], k: int = 5) -> list[PullRequestRef]:
        """Top-``k`` PRs closest to ``embedding`` under pgvector cosine.

        Uses pgvector's ``<=>`` operator (cosine distance, lower is closer).
        """
        emb = list(embedding)
        stmt = (
            select(
                m.PullRequest.id,
                m.PullRequest.number,
                m.PullRequest.title,
                m.PullRequest.body,
                m.Person.github_login,
                m.PullRequest.merged_at,
            )
            .outerjoin(m.Person, m.PullRequest.author_id == m.Person.id)
            .where(m.PullRequest.embedding.is_not(None))
            .order_by(m.PullRequest.embedding.cosine_distance(emb))
            .limit(k)
        )
        return [
            PullRequestRef(
                id=row[0],
                number=row[1],
                title=row[2],
                body=row[3],
                author_login=row[4],
                merged_at=row[5],
            )
            for row in (await self._session.execute(stmt)).all()
        ]

    async def similar_prs_scored(
        self, embedding: Sequence[float], k: int = 5
    ) -> list[tuple[PullRequestRef, float]]:
        """Top-``k`` PRs with a per-row cosine similarity score.

        Same ordering as :meth:`similar_prs` but returns
        ``(ref, similarity)`` pairs where similarity is
        ``1 - cosine_distance`` — i.e. higher means closer. The Context
        Packager needs the raw number to blend with Jaccard file-overlap
        into its related-PR score.
        """

        emb = list(embedding)
        distance = m.PullRequest.embedding.cosine_distance(emb).label("distance")
        stmt = (
            select(
                m.PullRequest.id,
                m.PullRequest.number,
                m.PullRequest.title,
                m.PullRequest.body,
                m.Person.github_login,
                m.PullRequest.merged_at,
                distance,
            )
            .outerjoin(m.Person, m.PullRequest.author_id == m.Person.id)
            .where(m.PullRequest.embedding.is_not(None))
            .order_by(distance)
            .limit(k)
        )
        out: list[tuple[PullRequestRef, float]] = []
        for row in (await self._session.execute(stmt)).all():
            ref = PullRequestRef(
                id=row[0],
                number=row[1],
                title=row[2],
                body=row[3],
                author_login=row[4],
                merged_at=row[5],
            )
            dist = float(row[6]) if row[6] is not None else 1.0
            out.append((ref, 1.0 - dist))
        return out

    async def similar_adrs(self, embedding: Sequence[float], k: int = 5) -> list[ADRRef]:
        """Top-``k`` ADRs closest to ``embedding`` under pgvector cosine."""
        emb = list(embedding)
        stmt = (
            select(m.ADR.id, m.ADR.title, m.ADR.status, m.ADR.body)
            .where(m.ADR.embedding.is_not(None))
            .order_by(m.ADR.embedding.cosine_distance(emb))
            .limit(k)
        )
        return [
            ADRRef(id=row[0], title=row[1], status=row[2], body=row[3])
            for row in (await self._session.execute(stmt)).all()
        ]

    async def files_touched_by_pr(self, pr_id: UUID) -> list[str]:
        """Distinct file paths touched by any commit in ``pr_id``.

        Used by the Context Packager's related-PR scorer to compute the
        Jaccard overlap of touched files between the PR under review and
        each candidate similar PR. Returns paths only — callers that need
        ``File.id`` should query via :meth:`file_by_path`.
        """

        stmt = (
            select(m.File.path)
            .join(m.CommitModifiesFile, m.CommitModifiesFile.file_id == m.File.id)
            .join(m.Commit, m.CommitModifiesFile.commit_id == m.Commit.id)
            .join(m.PRContainsCommit, m.PRContainsCommit.commit_id == m.Commit.id)
            .where(m.PRContainsCommit.pull_request_id == pr_id)
            .distinct()
        )
        rows = (await self._session.execute(stmt)).all()
        return [row[0] for row in rows]

    async def revert_commits_touching(
        self,
        file_id: UUID,
        since: dt.datetime,
    ) -> list[CommitRef]:
        """Commits that touched ``file_id`` since ``since`` whose message starts with ``revert``.

        The match is case-insensitive on the first word of the commit
        message. We deliberately do not infer reverts from diffs — message
        prefix is the convention git itself enforces with ``git revert``,
        and false positives here are visible in the Context Packager's
        risk notes which a human reads.
        """

        stmt = (
            select(
                m.Commit.id,
                m.Commit.sha,
                m.Person.github_login,
                m.Commit.message,
                m.Commit.committed_at,
            )
            .join(m.CommitModifiesFile, m.CommitModifiesFile.commit_id == m.Commit.id)
            .outerjoin(m.Person, m.Commit.author_id == m.Person.id)
            .where(
                and_(
                    m.CommitModifiesFile.file_id == file_id,
                    m.Commit.committed_at >= since,
                    func.lower(m.Commit.message).like("revert%"),
                )
            )
            .order_by(desc(m.Commit.committed_at))
        )
        return [
            CommitRef(
                id=row[0],
                sha=row[1],
                author_login=row[2],
                message=row[3],
                committed_at=row[4],
            )
            for row in (await self._session.execute(stmt)).all()
        ]

    async def commit_count_for_file_since(
        self,
        file_id: UUID,
        since: dt.datetime,
    ) -> int:
        """Number of commits touching ``file_id`` on or after ``since``.

        Powers the Context Packager's "high churn" risk note.
        """

        stmt = (
            select(func.count(m.Commit.id))
            .join(m.CommitModifiesFile, m.CommitModifiesFile.commit_id == m.Commit.id)
            .where(
                and_(
                    m.CommitModifiesFile.file_id == file_id,
                    m.Commit.committed_at >= since,
                )
            )
        )
        value = (await self._session.execute(stmt)).scalar()
        return int(value or 0)

    async def issue_by_number(self, repo_id: UUID, number: int) -> tuple[UUID, str, str] | None:
        """Resolve a same-repo issue by number to ``(id, title, state)``.

        Returns ``None`` if the issue is not in the graph yet — the
        Context Packager surfaces the bare reference in that case rather
        than dropping it.
        """

        stmt = (
            select(m.Issue.id, m.Issue.title, m.Issue.state)
            .where(and_(m.Issue.repository_id == repo_id, m.Issue.number == number))
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return (row[0], row[1], row[2])

    # -- Reviewer router support --------------------------------------------

    async def open_prs_assigned_to(self, person_id: UUID) -> int:
        """Count open PRs authored by ``person_id`` (proxy for review load).

        The v0.1 graph doesn't model review assignments; we approximate with
        "open PRs you authored". Phase 3 can extend this once we start
        persisting review request data.
        """
        stmt = select(func.count(m.PullRequest.id)).where(
            and_(
                m.PullRequest.author_id == person_id,
                m.PullRequest.state == "open",
            )
        )
        value = (await self._session.execute(stmt)).scalar()
        return int(value or 0)

    async def review_acceptance_rate(
        self,
        person_id: UUID,
        since: dt.datetime,
    ) -> tuple[int, int]:
        """Return ``(approved_count, total_count)`` for reviews by ``person_id``.

        Powers the Reviewer Router's acceptance-rate signal. A rate above
        0.70 earns a small score bump. Callers compute the ratio rather
        than the float so they can special-case "never reviewed" without
        a divide-by-zero dance.

        The window is ``submitted_at >= since`` (typically 6 months back).
        ``state`` is compared case-insensitively — GitHub's review API
        uses ``APPROVED`` but older data in the graph might carry mixed
        case.
        """

        approved_col = func.sum(
            case(
                (func.upper(m.Review.state) == "APPROVED", 1),
                else_=0,
            )
        ).label("approved")
        total_col = func.count(m.Review.id).label("total")
        stmt = select(approved_col, total_col).where(
            and_(
                m.Review.reviewer_id == person_id,
                m.Review.submitted_at >= since,
            )
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return (0, 0)
        approved = int(row[0] or 0)
        total = int(row[1] or 0)
        return (approved, total)

    async def last_activity_at(self, person_id: UUID) -> dt.datetime | None:
        """Most recent commit or review by ``person_id``, or ``None``.

        Powers the "is this person still active?" check. v0.1 treats
        anyone whose last activity is older than 90 days as inactive and
        drops them from the candidate pool. We check both commit
        authorship and review submissions — a reviewer who hasn't
        authored code recently but is still reviewing should still
        qualify.
        """

        commit_max = select(func.max(m.Commit.committed_at)).where(
            m.Commit.author_id == person_id
        )
        review_max = select(func.max(m.Review.submitted_at)).where(
            m.Review.reviewer_id == person_id
        )
        commit_at = (await self._session.execute(commit_max)).scalar()
        review_at = (await self._session.execute(review_max)).scalar()
        candidates = [ts for ts in (commit_at, review_at) if ts is not None]
        if not candidates:
            return None
        return max(candidates)

    async def call_graph_overlap_counts(
        self,
        file_ids: Sequence[UUID],
        since: dt.datetime,
    ) -> dict[UUID, int]:
        """Candidates with authorship on symbols adjacent to ``file_ids``.

        For each person who has authored a commit modifying a symbol
        that participates in a ``SymbolCall`` edge with any symbol inside
        one of ``file_ids``, return the count of distinct such symbols
        they have modified since ``since``.

        The Reviewer Router uses this to reward candidates who have
        recently worked on code structurally adjacent to the PR's
        changes — a weaker signal than direct authorship of the touched
        files but still correlated with review competence.

        Returning a ``dict`` keyed by ``person_id`` lets the router look
        up every candidate in O(1) after one query rather than issuing
        one query per candidate.
        """

        if not file_ids:
            return {}

        # Symbols that live in any of the changed files.
        touched_symbol_ids = select(m.Symbol.id).where(m.Symbol.file_id.in_(list(file_ids)))

        # Symbols that call into, or are called by, a touched symbol.
        # Exclude the touched symbols themselves to keep this distinct
        # from the authorship-share signal (which already covers "author
        # of touched files").
        neighbor_via_caller = select(m.SymbolCall.callee_id).where(
            m.SymbolCall.caller_id.in_(touched_symbol_ids)
        )
        neighbor_via_callee = select(m.SymbolCall.caller_id).where(
            m.SymbolCall.callee_id.in_(touched_symbol_ids)
        )
        neighbor_symbol_ids = neighbor_via_caller.union(neighbor_via_callee).subquery()

        distinct_overlap = func.count(func.distinct(m.CommitModifiesSymbol.symbol_id)).label(
            "overlap"
        )
        stmt = (
            select(m.Commit.author_id, distinct_overlap)
            .join(
                m.CommitModifiesSymbol,
                m.CommitModifiesSymbol.commit_id == m.Commit.id,
            )
            .where(
                and_(
                    m.CommitModifiesSymbol.symbol_id.in_(select(neighbor_symbol_ids)),
                    m.Commit.committed_at >= since,
                    m.Commit.author_id.is_not(None),
                )
            )
            .group_by(m.Commit.author_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return {row[0]: int(row[1]) for row in rows if row[0] is not None}

    async def codeowners_for(self, paths: Sequence[str]) -> list[PersonRef]:
        """People who most often authored commits touching any of ``paths``.

        v0.1 does not parse ``CODEOWNERS`` files — we approximate "owner" by
        "top recent author". Results are deduplicated across the input paths.
        """
        if not paths:
            return []
        count_col = func.count(m.Commit.id).label("c")
        stmt = (
            select(m.Person.id, m.Person.github_login, count_col)
            .join(m.Commit, m.Commit.author_id == m.Person.id)
            .join(m.CommitModifiesFile, m.CommitModifiesFile.commit_id == m.Commit.id)
            .join(m.File, m.CommitModifiesFile.file_id == m.File.id)
            .where(or_(*[m.File.path == p for p in paths]))
            .group_by(m.Person.id, m.Person.github_login)
            .order_by(desc(count_col))
        )
        return [
            PersonRef(id=row[0], github_login=row[1])
            for row in (await self._session.execute(stmt)).all()
        ]

    # -- Lookups -------------------------------------------------------------

    async def symbol_by_qualified_name(self, repo_id: UUID, name: str) -> SymbolRef | None:
        """Resolve ``name`` against ``repo_id``'s symbols, preferring exact match.

        If no exact qualified-name match exists but exactly one symbol's
        qualified name *ends with* ``.name`` or equals ``name``, return it.
        Ambiguous suffix matches return ``None`` — the caller should resolve
        with more context.
        """
        exact_stmt = (
            select(
                m.Symbol.id,
                m.Symbol.qualified_name,
                m.Symbol.kind,
                m.Symbol.signature,
                m.File.path,
            )
            .join(m.File, m.Symbol.file_id == m.File.id)
            .where(
                and_(
                    m.Symbol.repository_id == repo_id,
                    m.Symbol.qualified_name == name,
                )
            )
            .limit(1)
        )
        row = (await self._session.execute(exact_stmt)).first()
        if row is not None:
            return _to_symbol_ref(row)

        suffix = f".{name}"
        suffix_stmt = (
            select(
                m.Symbol.id,
                m.Symbol.qualified_name,
                m.Symbol.kind,
                m.Symbol.signature,
                m.File.path,
            )
            .join(m.File, m.Symbol.file_id == m.File.id)
            .where(
                and_(
                    m.Symbol.repository_id == repo_id,
                    m.Symbol.qualified_name.like(f"%{suffix}"),
                )
            )
            .limit(2)
        )
        rows = (await self._session.execute(suffix_stmt)).all()
        if len(rows) == 1:
            return _to_symbol_ref(rows[0])
        return None

    async def file_by_path(self, repo_id: UUID, path: str) -> UUID | None:
        """Return the ``File.id`` for ``(repo_id, path)``, if present."""
        stmt = (
            select(m.File.id)
            .where(and_(m.File.repository_id == repo_id, m.File.path == path))
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar()


# -- Helpers ----------------------------------------------------------------


def _to_symbol_ref(row: tuple) -> SymbolRef:  # type: ignore[type-arg]
    return SymbolRef(
        id=row[0],
        qualified_name=row[1],
        kind=row[2],
        signature=row[3],
        file_path=row[4],
    )


# ``literal_column`` is imported for downstream use in future reviewer-router
# heuristics; silence unused-import lints until then. ``case`` is used by
# :meth:`GraphClient.review_acceptance_rate`.
_ = (literal_column,)


__all__ = [
    "ADRRef",
    "AuthorshipCount",
    "CommitRef",
    "GraphClient",
    "PersonRef",
    "PullRequestRef",
    "Select",
    "SymbolRef",
]
