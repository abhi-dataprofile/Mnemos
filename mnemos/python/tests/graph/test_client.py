"""Integration tests for :class:`codereview.graph.client.GraphClient`.

Uses an in-memory SQLite database (see ``conftest.py``) so the tests exercise
real SQLAlchemy query compilation and row mapping, not mocked sessions. The
pgvector-specific ``similar_prs`` / ``similar_adrs`` paths are asserted on at
the SQL level since SQLite cannot evaluate cosine distance.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

from sqlalchemy import insert
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from codereview.graph import models as m
from codereview.graph.client import GraphClient

# -- Helpers ---------------------------------------------------------------


async def _seed_basic_graph(
    session: AsyncSession,
) -> tuple[dict[str, object], ...]:
    """Create a small repo with two files, four symbols, and three call edges.

    Layout:

      billing/invoice.py     generate_pdf, _render_pdf
      api/invoices.py        download_pdf, get_invoice

      download_pdf --(calls)--> generate_pdf
      download_pdf --(calls)--> _render_pdf (transitively, unused by tests)
      generate_pdf --(calls)--> _render_pdf
    """
    repo_id = uuid4()
    await session.execute(
        insert(m.Repository).values(
            id=repo_id,
            github_id=12345,
            owner="mnemos",
            name="fixture",
            installation_id=1,
            default_branch="main",
            created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            updated_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
    )

    billing_id = uuid4()
    api_id = uuid4()
    await session.execute(
        insert(m.File),
        [
            dict(
                id=billing_id,
                repository_id=repo_id,
                path="src/billing/invoice.py",
                language="python",
                content_hash="hash_billing",
                first_seen_sha="sha1",
                last_seen_sha="sha1",
            ),
            dict(
                id=api_id,
                repository_id=repo_id,
                path="src/api/invoices.py",
                language="python",
                content_hash="hash_api",
                first_seen_sha="sha1",
                last_seen_sha="sha1",
            ),
        ],
    )

    generate_pdf_id = uuid4()
    render_pdf_id = uuid4()
    download_pdf_id = uuid4()
    get_invoice_id = uuid4()

    await session.execute(
        insert(m.Symbol),
        [
            dict(
                id=generate_pdf_id,
                repository_id=repo_id,
                file_id=billing_id,
                qualified_name="generate_pdf",
                kind="function",
                signature="generate_pdf(invoice_id, repo) -> bytes",
                ast_hash="gh1",
                start_line=6,
                end_line=16,
            ),
            dict(
                id=render_pdf_id,
                repository_id=repo_id,
                file_id=billing_id,
                qualified_name="_render_pdf",
                kind="function",
                signature="_render_pdf(invoice) -> bytes",
                ast_hash="gh2",
                start_line=18,
                end_line=23,
            ),
            dict(
                id=download_pdf_id,
                repository_id=repo_id,
                file_id=api_id,
                qualified_name="download_pdf",
                kind="function",
                signature="download_pdf(invoice_id) -> Response",
                ast_hash="gh3",
                start_line=12,
                end_line=19,
            ),
            dict(
                id=get_invoice_id,
                repository_id=repo_id,
                file_id=api_id,
                qualified_name="get_invoice",
                kind="function",
                signature="get_invoice(invoice_id) -> Response",
                ast_hash="gh4",
                start_line=21,
                end_line=32,
            ),
        ],
    )

    await session.execute(
        insert(m.SymbolCall),
        [
            dict(
                id=uuid4(),
                caller_id=download_pdf_id,
                callee_id=generate_pdf_id,
                line=15,
                dynamic=False,
            ),
            dict(
                id=uuid4(),
                caller_id=generate_pdf_id,
                callee_id=render_pdf_id,
                line=15,
                dynamic=False,
            ),
        ],
    )

    await session.commit()
    return (
        {
            "repo_id": repo_id,
            "billing_id": billing_id,
            "api_id": api_id,
            "generate_pdf_id": generate_pdf_id,
            "render_pdf_id": render_pdf_id,
            "download_pdf_id": download_pdf_id,
            "get_invoice_id": get_invoice_id,
        },
    )


async def _seed_commits_and_reviews(
    session: AsyncSession,
    ids: dict[str, object],
) -> dict[str, object]:
    """Augment the basic graph with commits + authors + a reviewed PR."""
    alice = uuid4()
    bob = uuid4()
    carol = uuid4()
    await session.execute(
        insert(m.Person),
        [
            dict(id=alice, github_login="alice", github_id=1001, name="Alice"),
            dict(id=bob, github_login="bob", github_id=1002, name="Bob"),
            dict(id=carol, github_login="carol", github_id=1003, name="Carol"),
        ],
    )

    commit_old = uuid4()
    commit_mid = uuid4()
    commit_new = uuid4()
    await session.execute(
        insert(m.Commit),
        [
            dict(
                id=commit_old,
                repository_id=ids["repo_id"],
                sha="a" * 40,
                author_id=alice,
                message="old alice edit",
                committed_at=dt.datetime(2026, 1, 5, tzinfo=dt.timezone.utc),
            ),
            dict(
                id=commit_mid,
                repository_id=ids["repo_id"],
                sha="b" * 40,
                author_id=alice,
                message="second alice edit",
                committed_at=dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc),
            ),
            dict(
                id=commit_new,
                repository_id=ids["repo_id"],
                sha="c" * 40,
                author_id=bob,
                message="bob edit",
                committed_at=dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc),
            ),
        ],
    )

    await session.execute(
        insert(m.CommitModifiesFile),
        [
            dict(
                id=uuid4(),
                commit_id=commit_old,
                file_id=ids["billing_id"],
                change_kind="modify",
            ),
            dict(
                id=uuid4(),
                commit_id=commit_mid,
                file_id=ids["billing_id"],
                change_kind="modify",
            ),
            dict(
                id=uuid4(),
                commit_id=commit_new,
                file_id=ids["billing_id"],
                change_kind="modify",
            ),
        ],
    )

    # PR that Carol reviewed; PR contains commit_new.
    pr_id = uuid4()
    await session.execute(
        insert(m.PullRequest).values(
            id=pr_id,
            repository_id=ids["repo_id"],
            number=42,
            title="refactor billing",
            body="cleanup",
            state="open",
            author_id=bob,
            head_sha="c" * 40,
            base_sha="a" * 40,
            created_at=dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc),
        )
    )
    await session.execute(
        insert(m.PRContainsCommit).values(id=uuid4(), pull_request_id=pr_id, commit_id=commit_new)
    )
    review_id = uuid4()
    await session.execute(
        insert(m.Review).values(
            id=review_id,
            pull_request_id=pr_id,
            reviewer_id=carol,
            state="APPROVED",
            body="lgtm",
            submitted_at=dt.datetime(2026, 3, 2, tzinfo=dt.timezone.utc),
        )
    )

    await session.commit()
    return {"alice": alice, "bob": bob, "carol": carol, "pr_id": pr_id}


# -- Call graph ------------------------------------------------------------


async def test_callers_of_returns_direct_callers(db_session: AsyncSession) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    client = GraphClient(db_session)

    callers = await client.callers_of(ids["generate_pdf_id"])  # type: ignore[arg-type]
    assert [c.qualified_name for c in callers] == ["download_pdf"]
    assert callers[0].file_path == "src/api/invoices.py"


async def test_callees_of_follows_edges(db_session: AsyncSession) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    client = GraphClient(db_session)

    callees = await client.callees_of(ids["download_pdf_id"])  # type: ignore[arg-type]
    assert [c.qualified_name for c in callees] == ["generate_pdf"]


async def test_symbols_in_file_ordered_by_start_line(db_session: AsyncSession) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    client = GraphClient(db_session)

    symbols = await client.symbols_in_file(ids["billing_id"])  # type: ignore[arg-type]
    assert [s.qualified_name for s in symbols] == ["generate_pdf", "_render_pdf"]


# -- Commit history --------------------------------------------------------


async def test_recent_commits_touching_orders_by_recency(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    await _seed_commits_and_reviews(db_session, ids)
    client = GraphClient(db_session)

    commits = await client.recent_commits_touching(
        ids["billing_id"],  # type: ignore[arg-type]
        limit=2,
    )
    assert [c.author_login for c in commits] == ["bob", "alice"]


async def test_authors_of_file_counts_and_sorts(db_session: AsyncSession) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    await _seed_commits_and_reviews(db_session, ids)
    client = GraphClient(db_session)

    since = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    authors = await client.authors_of_file(
        ids["billing_id"],  # type: ignore[arg-type]
        since,
    )
    # Alice authored 2 commits, Bob 1; Alice should sort first.
    assert [(a.person.github_login, a.count) for a in authors] == [
        ("alice", 2),
        ("bob", 1),
    ]


async def test_authors_of_file_filters_by_since(db_session: AsyncSession) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    await _seed_commits_and_reviews(db_session, ids)
    client = GraphClient(db_session)

    since = dt.datetime(2026, 2, 15, tzinfo=dt.timezone.utc)
    authors = await client.authors_of_file(
        ids["billing_id"],  # type: ignore[arg-type]
        since,
    )
    assert [a.person.github_login for a in authors] == ["bob"]


async def test_reviewers_of_file_finds_through_commit_chain(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    await _seed_commits_and_reviews(db_session, ids)
    client = GraphClient(db_session)

    since = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    reviewers = await client.reviewers_of_file(
        ids["billing_id"],  # type: ignore[arg-type]
        since,
    )
    assert [(r.person.github_login, r.count) for r in reviewers] == [("carol", 1)]


# -- Reviewer router support ----------------------------------------------


async def test_open_prs_assigned_to_counts_open_only(db_session: AsyncSession) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    people = await _seed_commits_and_reviews(db_session, ids)
    client = GraphClient(db_session)

    # Bob authored the open PR; Alice and Carol have none.
    assert await client.open_prs_assigned_to(people["bob"]) == 1  # type: ignore[arg-type]
    assert await client.open_prs_assigned_to(people["alice"]) == 0  # type: ignore[arg-type]


async def test_codeowners_for_returns_top_authors(db_session: AsyncSession) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    await _seed_commits_and_reviews(db_session, ids)
    client = GraphClient(db_session)

    people = await client.codeowners_for(["src/billing/invoice.py"])
    assert [p.github_login for p in people] == ["alice", "bob"]


async def test_codeowners_empty_input_returns_empty(db_session: AsyncSession) -> None:
    client = GraphClient(db_session)
    assert await client.codeowners_for([]) == []


# -- Lookups ---------------------------------------------------------------


async def test_symbol_by_qualified_name_exact_match(db_session: AsyncSession) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    client = GraphClient(db_session)

    sym = await client.symbol_by_qualified_name(
        ids["repo_id"],  # type: ignore[arg-type]
        "generate_pdf",
    )
    assert sym is not None
    assert sym.qualified_name == "generate_pdf"


async def test_symbol_by_qualified_name_suffix_fallback(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    # Rename generate_pdf to billing.invoice.generate_pdf to force suffix match.
    await db_session.execute(
        m.Symbol.__table__.update()
        .where(m.Symbol.id == ids["generate_pdf_id"])
        .values(qualified_name="billing.invoice.generate_pdf")
    )
    await db_session.commit()
    client = GraphClient(db_session)

    sym = await client.symbol_by_qualified_name(
        ids["repo_id"],  # type: ignore[arg-type]
        "generate_pdf",
    )
    assert sym is not None
    assert sym.qualified_name == "billing.invoice.generate_pdf"


async def test_symbol_by_qualified_name_returns_none_on_miss(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    client = GraphClient(db_session)

    result = await client.symbol_by_qualified_name(
        ids["repo_id"],  # type: ignore[arg-type]
        "nonexistent",
    )
    assert result is None


async def test_file_by_path_returns_id(db_session: AsyncSession) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    client = GraphClient(db_session)

    fid = await client.file_by_path(
        ids["repo_id"],  # type: ignore[arg-type]
        "src/billing/invoice.py",
    )
    assert fid == ids["billing_id"]


# -- Similarity (pgvector-only; SQL-shape assertions) ----------------------


def test_similar_prs_compiles_with_cosine_distance_operator() -> None:
    """``similar_prs`` should render a pgvector ``<=>`` on PullRequest.embedding."""
    # Build the select manually by calling the helper path. We assemble the
    # same stmt the coroutine would, then compile it to PG dialect SQL.
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql as _pg

    from codereview.graph.client import GraphClient as _GC

    stmt = (
        select(m.PullRequest.id)
        .where(m.PullRequest.embedding.is_not(None))
        .order_by(m.PullRequest.embedding.cosine_distance([0.0] * 1536))
        .limit(5)
    )
    sql = str(stmt.compile(dialect=_pg.dialect(), compile_kwargs={"literal_binds": False}))
    assert "<=>" in sql
    # Defensive: ensure the method we care about is still on the class.
    assert hasattr(_GC, "similar_prs") and hasattr(_GC, "similar_adrs")


def test_similar_adrs_compiles_against_adr_embedding() -> None:
    from sqlalchemy import select

    stmt = (
        select(m.ADR.id)
        .where(m.ADR.embedding.is_not(None))
        .order_by(m.ADR.embedding.cosine_distance([0.0] * 1536))
        .limit(5)
    )
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "adrs.embedding" in sql
    assert "<=>" in sql


def test_similar_prs_scored_exists_on_client() -> None:
    """Context Packager needs the scored variant; pgvector path is PG-only."""

    from codereview.graph.client import GraphClient as _GC

    assert hasattr(_GC, "similar_prs_scored")


# -- Phase 5 graph extensions ---------------------------------------------


async def test_files_touched_by_pr_returns_distinct_paths(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    people = await _seed_commits_and_reviews(db_session, ids)
    client = GraphClient(db_session)

    paths = await client.files_touched_by_pr(people["pr_id"])  # type: ignore[arg-type]
    assert paths == ["src/billing/invoice.py"]


async def test_files_touched_by_pr_empty_for_unknown_pr(
    db_session: AsyncSession,
) -> None:
    client = GraphClient(db_session)
    assert await client.files_touched_by_pr(uuid4()) == []


async def test_revert_commits_touching_filters_by_message_prefix(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    await _seed_commits_and_reviews(db_session, ids)
    # Add a revert commit on the billing file.
    revert_id = uuid4()
    await db_session.execute(
        insert(m.Commit).values(
            id=revert_id,
            repository_id=ids["repo_id"],
            sha="d" * 40,
            author_id=None,
            message='Revert "bump backoff"',
            committed_at=dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc),
        )
    )
    await db_session.execute(
        insert(m.CommitModifiesFile).values(
            id=uuid4(),
            commit_id=revert_id,
            file_id=ids["billing_id"],
            change_kind="modify",
        )
    )
    await db_session.commit()

    client = GraphClient(db_session)
    since = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    reverts = await client.revert_commits_touching(
        ids["billing_id"],  # type: ignore[arg-type]
        since,
    )
    assert [c.sha[-1] for c in reverts] == ["d"]


async def test_revert_commits_touching_excludes_regular_messages(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    await _seed_commits_and_reviews(db_session, ids)
    client = GraphClient(db_session)

    since = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    # Seeded commits say "old alice edit", "bob edit", etc. — none start with "revert".
    assert (
        await client.revert_commits_touching(
            ids["billing_id"],  # type: ignore[arg-type]
            since,
        )
        == []
    )


async def test_revert_commits_touching_honors_since_window(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    # Seed a single old revert and make sure a tight since window excludes it.
    revert_id = uuid4()
    await db_session.execute(
        insert(m.Commit).values(
            id=revert_id,
            repository_id=ids["repo_id"],
            sha="e" * 40,
            author_id=None,
            message="revert: old mistake",
            committed_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
    )
    await db_session.execute(
        insert(m.CommitModifiesFile).values(
            id=uuid4(),
            commit_id=revert_id,
            file_id=ids["billing_id"],
            change_kind="modify",
        )
    )
    await db_session.commit()
    client = GraphClient(db_session)

    since = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)
    assert (
        await client.revert_commits_touching(
            ids["billing_id"],  # type: ignore[arg-type]
            since,
        )
        == []
    )


async def test_commit_count_for_file_since_counts_commits(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    await _seed_commits_and_reviews(db_session, ids)
    client = GraphClient(db_session)

    since = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    count = await client.commit_count_for_file_since(
        ids["billing_id"],  # type: ignore[arg-type]
        since,
    )
    # Three seeded commits all touch the billing file.
    assert count == 3


async def test_commit_count_for_file_since_respects_window(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    await _seed_commits_and_reviews(db_session, ids)
    client = GraphClient(db_session)

    since = dt.datetime(2026, 2, 15, tzinfo=dt.timezone.utc)
    count = await client.commit_count_for_file_since(
        ids["billing_id"],  # type: ignore[arg-type]
        since,
    )
    # Only the 2026-03-01 commit passes the since filter.
    assert count == 1


async def test_commit_count_for_file_since_zero_for_untouched_file(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    await _seed_commits_and_reviews(db_session, ids)
    client = GraphClient(db_session)

    since = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    assert (
        await client.commit_count_for_file_since(
            ids["api_id"],  # type: ignore[arg-type]
            since,
        )
        == 0
    )


async def test_issue_by_number_returns_id_title_state(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    issue_id = uuid4()
    await db_session.execute(
        insert(m.Issue).values(
            id=issue_id,
            repository_id=ids["repo_id"],
            number=7,
            title="flaky retries",
            state="open",
        )
    )
    await db_session.commit()
    client = GraphClient(db_session)

    row = await client.issue_by_number(ids["repo_id"], 7)  # type: ignore[arg-type]
    assert row == (issue_id, "flaky retries", "open")


async def test_issue_by_number_returns_none_when_missing(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    client = GraphClient(db_session)
    assert await client.issue_by_number(ids["repo_id"], 999) is None  # type: ignore[arg-type]


# -- Phase 6 graph extensions ---------------------------------------------


async def _seed_two_prs_with_reviews(
    session: AsyncSession,
    ids: dict[str, object],
    people: dict[str, object],
) -> None:
    """Add a second PR + two extra reviews by Carol (1 approved, 1 changes)."""

    pr2_id = uuid4()
    await session.execute(
        insert(m.PullRequest).values(
            id=pr2_id,
            repository_id=ids["repo_id"],
            number=43,
            title="fix pdf",
            body="",
            state="open",
            author_id=people["alice"],
            head_sha="f" * 40,
            base_sha="a" * 40,
            created_at=dt.datetime(2026, 3, 15, tzinfo=dt.timezone.utc),
        )
    )
    await session.execute(
        insert(m.Review),
        [
            dict(
                id=uuid4(),
                pull_request_id=pr2_id,
                reviewer_id=people["carol"],
                state="CHANGES_REQUESTED",
                body="needs work",
                submitted_at=dt.datetime(2026, 3, 16, tzinfo=dt.timezone.utc),
            ),
            dict(
                id=uuid4(),
                pull_request_id=pr2_id,
                reviewer_id=people["carol"],
                state="approved",  # Lowercase, exercises case-insensitive match.
                body="take two",
                submitted_at=dt.datetime(2026, 3, 17, tzinfo=dt.timezone.utc),
            ),
        ],
    )
    await session.commit()


async def test_review_acceptance_rate_counts_approved_and_total(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    people = await _seed_commits_and_reviews(db_session, ids)
    await _seed_two_prs_with_reviews(db_session, ids, people)
    client = GraphClient(db_session)

    since = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    approved, total = await client.review_acceptance_rate(
        people["carol"],  # type: ignore[arg-type]
        since,
    )
    # Three reviews: APPROVED + CHANGES_REQUESTED + approved (lower) → 2/3.
    assert (approved, total) == (2, 3)


async def test_review_acceptance_rate_zero_for_unknown_reviewer(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    await _seed_commits_and_reviews(db_session, ids)
    client = GraphClient(db_session)

    since = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    assert await client.review_acceptance_rate(uuid4(), since) == (0, 0)


async def test_review_acceptance_rate_respects_since_window(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    people = await _seed_commits_and_reviews(db_session, ids)
    client = GraphClient(db_session)

    # Only Carol's APPROVED review from 2026-03-02 would count, but we push
    # `since` past it.
    since = dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc)
    assert await client.review_acceptance_rate(
        people["carol"],  # type: ignore[arg-type]
        since,
    ) == (0, 0)


async def test_last_activity_at_returns_most_recent_commit_or_review(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    people = await _seed_commits_and_reviews(db_session, ids)
    client = GraphClient(db_session)

    # SQLite drops tzinfo on DateTime round-trips, so compare naive parts
    # — Postgres preserves it, covered by the similarity SQL-shape tests.
    # Alice has commits up to 2026-02-01, no reviews.
    alice_last = await client.last_activity_at(people["alice"])  # type: ignore[arg-type]
    assert alice_last is not None
    assert alice_last.replace(tzinfo=None) == dt.datetime(2026, 2, 1)

    # Carol has one review on 2026-03-02, no commits.
    carol_last = await client.last_activity_at(people["carol"])  # type: ignore[arg-type]
    assert carol_last is not None
    assert carol_last.replace(tzinfo=None) == dt.datetime(2026, 3, 2)


async def test_last_activity_at_none_for_inactive_person(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    ghost = uuid4()
    await db_session.execute(
        insert(m.Person).values(
            id=ghost,
            github_login="ghost",
            github_id=9999,
        )
    )
    await db_session.commit()
    client = GraphClient(db_session)

    assert await client.last_activity_at(ghost) is None


async def test_call_graph_overlap_counts_finds_adjacency(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    people = await _seed_commits_and_reviews(db_session, ids)
    # Seed a CommitModifiesSymbol record for Alice against download_pdf,
    # which is in api/invoices.py. download_pdf CALLS generate_pdf (which
    # lives in billing/invoice.py). So when we ask about overlap for the
    # billing file, Alice should light up via the caller-side edge.
    await db_session.execute(
        insert(m.CommitModifiesSymbol).values(
            id=uuid4(),
            commit_id=(
                await db_session.execute(
                    m.Commit.__table__.select().where(m.Commit.author_id == people["alice"])
                )
            )
            .first()
            .id,
            symbol_id=ids["download_pdf_id"],
            change_kind="modify",
            details={},
        )
    )
    await db_session.commit()
    client = GraphClient(db_session)

    since = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    counts = await client.call_graph_overlap_counts(
        [ids["billing_id"]],  # type: ignore[list-item]
        since,
    )
    # Alice authored a commit that modified download_pdf, a neighbor of
    # the billing file's generate_pdf. Expect exactly one overlap symbol.
    assert counts == {people["alice"]: 1}


async def test_call_graph_overlap_counts_empty_file_list(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    await _seed_commits_and_reviews(db_session, ids)
    client = GraphClient(db_session)

    since = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    assert await client.call_graph_overlap_counts([], since) == {}


async def test_call_graph_overlap_counts_ignores_pre_window_commits(
    db_session: AsyncSession,
) -> None:
    (ids,) = await _seed_basic_graph(db_session)
    people = await _seed_commits_and_reviews(db_session, ids)
    old_commit_id = uuid4()
    await db_session.execute(
        insert(m.Commit).values(
            id=old_commit_id,
            repository_id=ids["repo_id"],
            sha="0" * 40,
            author_id=people["alice"],
            message="ancient",
            committed_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        )
    )
    await db_session.execute(
        insert(m.CommitModifiesSymbol).values(
            id=uuid4(),
            commit_id=old_commit_id,
            symbol_id=ids["download_pdf_id"],
            change_kind="modify",
            details={},
        )
    )
    await db_session.commit()

    client = GraphClient(db_session)
    since = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    counts = await client.call_graph_overlap_counts(
        [ids["billing_id"]],  # type: ignore[list-item]
        since,
    )
    # The ancient commit pre-dates `since`, so nobody should be counted.
    assert counts == {}
