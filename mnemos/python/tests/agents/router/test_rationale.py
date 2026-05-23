"""Unit tests for :mod:`codereview.agents.router.rationale`.

The rationale layer is a deterministic ``if / elif`` chain keyed off the
scoring thresholds. These tests walk each precedence branch so a future
refactor that changes the phrasing or the precedence order has to
explicitly own the change.
"""

from __future__ import annotations

from uuid import uuid4

from codereview.agents.router.rationale import rationale
from codereview.agents.router.score import (
    ACCEPTANCE_RATE_THRESHOLD,
    AUTHORSHIP_SHARE_THRESHOLD,
    CALL_GRAPH_OVERLAP_THRESHOLD,
    LOAD_LOW_THRESHOLD,
    RECENT_REVIEW_THRESHOLD,
)
from codereview.agents.router.types import Candidate, Signals


def _person() -> Candidate:
    return Candidate(login="alice", person_id=uuid4())


def _team() -> Candidate:
    return Candidate(login="acme/billing", is_team=True)


# -- Precedence: CODEOWNERS -----------------------------------------------


def test_codeowner_rationale_names_authored_path_when_available() -> None:
    sig = Signals(
        is_codeowner=True,
        authored_files=("src/billing/invoice.py", "src/billing/refund.py"),
    )
    # The first authored file is quoted to anchor the phrasing in a
    # concrete stake.
    assert rationale(_person(), sig) == "CODEOWNERS match for src/billing/invoice.py."


def test_codeowner_rationale_falls_back_when_no_authored_files() -> None:
    # Team CODEOWNERS entries typically have no authored_files.
    sig = Signals(is_codeowner=True)
    assert (
        rationale(_team(), sig)
        == "CODEOWNERS match for the files touched by this PR."
    )


def test_codeowner_rationale_wins_over_authorship() -> None:
    # Even though authorship threshold is met, CODEOWNERS is the stronger
    # signal and should take precedence.
    sig = Signals(
        is_codeowner=True,
        authorship_share=0.95,
        authored_files=("src/a.py",),
    )
    out = rationale(_person(), sig)
    assert out.startswith("CODEOWNERS match")
    assert "Authored" not in out


# -- Precedence: authorship ---------------------------------------------


def test_authorship_rationale_reports_rounded_percentage() -> None:
    # 40% authorship with no CODEOWNERS flag.
    sig = Signals(authorship_share=0.4)
    assert (
        rationale(_person(), sig)
        == "Authored 40% of touched files in the last 6 months."
    )


def test_authorship_just_at_threshold_qualifies() -> None:
    sig = Signals(authorship_share=AUTHORSHIP_SHARE_THRESHOLD)
    assert "Authored" in rationale(_person(), sig)


# -- Precedence: call-graph overlap -------------------------------------


def test_call_graph_rationale_singular_vs_plural() -> None:
    singular = Signals(call_graph_overlap=1)
    plural = Signals(call_graph_overlap=3)
    assert "1 symbol that" in rationale(_person(), singular)
    assert "3 symbols that" in rationale(_person(), plural)


def test_call_graph_rationale_at_threshold_qualifies() -> None:
    sig = Signals(call_graph_overlap=CALL_GRAPH_OVERLAP_THRESHOLD)
    assert "Recently modified" in rationale(_person(), sig)


# -- Precedence: review volume + acceptance -----------------------------


def test_high_acceptance_rate_rationale_mentions_both_counts() -> None:
    sig = Signals(
        recent_review_count=5,
        total_reviews=10,
        review_acceptance_rate=0.9,
    )
    out = rationale(_person(), sig)
    assert "Reviewed 5 PRs" in out
    assert "90%" in out
    assert "acceptance rate" in out


def test_acceptance_branch_requires_total_reviews_nonzero() -> None:
    # Edge case: review_acceptance_rate could be artificially set high
    # without any underlying reviews. The branch guards on
    # total_reviews>0 so it falls through to the plain count branch.
    sig = Signals(
        recent_review_count=RECENT_REVIEW_THRESHOLD,
        total_reviews=0,
        review_acceptance_rate=1.0,  # nonsense without reviews
    )
    out = rationale(_person(), sig)
    assert "acceptance rate" not in out
    assert "Reviewed" in out  # falls through to plain review-count branch


def test_plain_review_count_rationale_when_acceptance_below_threshold() -> None:
    sig = Signals(
        recent_review_count=4,
        total_reviews=4,
        review_acceptance_rate=ACCEPTANCE_RATE_THRESHOLD - 0.01,
    )
    out = rationale(_person(), sig)
    assert "acceptance rate" not in out
    assert out == "Reviewed 4 PRs touching these files recently."


# -- Precedence: capacity fallback --------------------------------------


def test_capacity_rationale_singular_vs_plural() -> None:
    # Strict-less-than on LOAD_LOW_THRESHOLD (default 3) means 0, 1, 2
    # fire. Pluralisation flips at 1.
    assert rationale(_person(), Signals(open_pr_load=1)).endswith("1 open PR).")
    assert rationale(_person(), Signals(open_pr_load=0)).endswith("0 open PRs).")
    assert rationale(_person(), Signals(open_pr_load=2)).endswith("2 open PRs).")


def test_capacity_only_fires_strictly_below_threshold() -> None:
    # At the threshold itself, capacity doesn't fire — we fall through
    # to the absolute fallback (no other signals set).
    sig = Signals(open_pr_load=LOAD_LOW_THRESHOLD)
    assert rationale(_person(), sig) == (
        "Surfaced by historical review activity near this PR."
    )


# -- Precedence: team fallback ------------------------------------------


def test_team_fallback_fires_when_nothing_else_matches() -> None:
    # A team CODEOWNERS entry could reach the rationale layer with
    # is_codeowner=False if, e.g., the CODEOWNERS map didn't name this
    # path but the team was in the pool for some other reason.
    # Additionally, capacity is zeroed out because open_pr_load defaults
    # to 0 — so we push load up to the threshold to skip that branch.
    sig = Signals(open_pr_load=LOAD_LOW_THRESHOLD)
    out = rationale(_team(), sig)
    assert out == "Listed as a team owner (acme/billing)."


# -- Absolute fallback --------------------------------------------------


def test_absolute_fallback_for_person_without_signals() -> None:
    # Non-team candidate, no signals at all, neutral load.
    sig = Signals(open_pr_load=LOAD_LOW_THRESHOLD)
    assert rationale(_person(), sig) == (
        "Surfaced by historical review activity near this PR."
    )
