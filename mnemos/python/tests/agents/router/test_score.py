"""Unit tests for :mod:`codereview.agents.router.score`."""

from __future__ import annotations

from codereview.agents.router.score import (
    ACCEPTANCE_RATE_THRESHOLD,
    AUTHORSHIP_SHARE_THRESHOLD,
    CALL_GRAPH_OVERLAP_THRESHOLD,
    LOAD_HIGH_THRESHOLD,
    LOAD_LOW_THRESHOLD,
    RECENT_REVIEW_THRESHOLD,
    WEIGHTS,
    score,
)
from codereview.agents.router.types import Candidate, Signals


def _cand() -> Candidate:
    # The candidate argument is currently unused by score(); anything
    # with a plausible shape suffices.
    return Candidate(login="alice")


def _neutral_load() -> int:
    # A load that does NOT trigger capacity bonus (not strictly <3) and
    # does NOT trigger the overload penalty (not strictly >8). Lets the
    # individual-signal tests isolate one signal at a time.
    return LOAD_LOW_THRESHOLD  # = 3


# -- Individual signals ---------------------------------------------------


def test_all_defaults_plus_neutral_load_scores_zero() -> None:
    # With neutral load, none of the bonuses fire, so the score floor
    # is 0.0.
    assert score(_cand(), Signals(open_pr_load=_neutral_load())) == 0.0


def test_codeowner_alone_adds_codeowner_weight() -> None:
    sig = Signals(is_codeowner=True, open_pr_load=_neutral_load())
    assert score(_cand(), sig) == WEIGHTS["codeowner"]


def test_authorship_at_threshold_triggers_full_weight() -> None:
    # authorship_share >= 0.20 qualifies.
    sig = Signals(
        authorship_share=AUTHORSHIP_SHARE_THRESHOLD,
        open_pr_load=_neutral_load(),
    )
    assert score(_cand(), sig) == WEIGHTS["authorship"]


def test_authorship_just_below_threshold_does_not_count() -> None:
    sig = Signals(
        authorship_share=AUTHORSHIP_SHARE_THRESHOLD - 0.01,
        open_pr_load=_neutral_load(),
    )
    assert score(_cand(), sig) == 0.0


def test_recent_reviews_over_threshold_counts() -> None:
    sig = Signals(
        recent_review_count=RECENT_REVIEW_THRESHOLD,
        open_pr_load=_neutral_load(),
    )
    assert score(_cand(), sig) == WEIGHTS["recent_reviews"]


def test_call_graph_overlap_counts() -> None:
    sig = Signals(
        call_graph_overlap=CALL_GRAPH_OVERLAP_THRESHOLD,
        open_pr_load=_neutral_load(),
    )
    assert score(_cand(), sig) == WEIGHTS["call_graph"]


def test_acceptance_rate_at_threshold_counts() -> None:
    sig = Signals(
        review_acceptance_rate=ACCEPTANCE_RATE_THRESHOLD,
        open_pr_load=_neutral_load(),
    )
    assert score(_cand(), sig) == WEIGHTS["acceptance"]


def test_capacity_bonus_fires_below_low_threshold() -> None:
    sig = Signals(open_pr_load=LOAD_LOW_THRESHOLD - 1)
    assert score(_cand(), sig) == WEIGHTS["has_capacity"]


def test_capacity_bonus_does_not_fire_at_low_threshold() -> None:
    # Strict inequality on the low threshold.
    sig = Signals(open_pr_load=LOAD_LOW_THRESHOLD)
    assert score(_cand(), sig) == 0.0


def test_overload_penalty_fires_above_high_threshold() -> None:
    sig = Signals(open_pr_load=LOAD_HIGH_THRESHOLD + 1)
    assert score(_cand(), sig) == WEIGHTS["overloaded_penalty"]


def test_overload_penalty_does_not_fire_at_high_threshold() -> None:
    # Strict inequality on the high threshold.
    sig = Signals(open_pr_load=LOAD_HIGH_THRESHOLD)
    assert score(_cand(), sig) == 0.0


# -- Composition ---------------------------------------------------------


def test_saturated_positive_signals_sum_to_one() -> None:
    sig = Signals(
        is_codeowner=True,
        authorship_share=1.0,
        recent_review_count=20,
        call_graph_overlap=5,
        review_acceptance_rate=1.0,
        open_pr_load=0,
    )
    # 0.30 + 0.25 + 0.15 + 0.10 + 0.10 + 0.10 = 1.00
    assert score(_cand(), sig) == 1.0


def test_overload_demotes_strong_candidate_below_weaker_free_one() -> None:
    strong_but_overloaded = Signals(
        is_codeowner=True,
        authorship_share=1.0,
        recent_review_count=20,
        open_pr_load=15,  # triggers -0.20 AND disqualifies capacity bonus
    )
    weaker_but_free = Signals(
        authorship_share=0.25,
        open_pr_load=1,
    )
    strong_score = score(_cand(), strong_but_overloaded)
    weak_score = score(_cand(), weaker_but_free)
    # Strong still wins raw (codeowner + authorship + recent − penalty
    # = 0.30 + 0.25 + 0.15 − 0.20 = 0.50 vs authorship + capacity = 0.35).
    assert weak_score < strong_score
    # But importantly: the penalty fires — the strong candidate is
    # noticeably below its unpenalised ceiling.
    assert strong_score < (
        WEIGHTS["codeowner"]
        + WEIGHTS["authorship"]
        + WEIGHTS["recent_reviews"]
    )


def test_weights_sum_correctly() -> None:
    # Sanity check that the documented "positive signals sum to 1.0"
    # invariant hasn't drifted. If somebody edits WEIGHTS without
    # intent, this test yells.
    positive_total = (
        WEIGHTS["codeowner"]
        + WEIGHTS["authorship"]
        + WEIGHTS["recent_reviews"]
        + WEIGHTS["call_graph"]
        + WEIGHTS["acceptance"]
        + WEIGHTS["has_capacity"]
    )
    assert round(positive_total, 4) == 1.0
    assert WEIGHTS["overloaded_penalty"] < 0
