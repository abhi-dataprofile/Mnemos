"""Scoring function for the Reviewer Router.

The weights here are pulled from plan doc § 3. They are intentionally
round numbers rather than calibrated from data — v0.1 is aiming for
"obviously right on the common cases", not a tuned recommender. The
weights live here (not inlined in the agent) so Phase 8 telemetry can
swap them without touching the agent wiring.

Rules of thumb encoded below:

- A CODEOWNERS match is the strongest single positive signal.
- Owning a fifth or more of the touched files is nearly as strong.
- Recent review activity, call-graph overlap, and a high acceptance
  rate each earn a small bump.
- Having capacity (<3 open PRs) is a small bonus; being drowning
  (>8 open PRs) is a substantial penalty.

The function is pure — no graph access, no I/O — so it is trivially
unit-testable with synthetic :class:`Signals`.
"""

from __future__ import annotations

from codereview.agents.router.types import Candidate, Signals

__all__ = [
    "ACCEPTANCE_RATE_THRESHOLD",
    "AUTHORSHIP_SHARE_THRESHOLD",
    "CALL_GRAPH_OVERLAP_THRESHOLD",
    "LOAD_HIGH_THRESHOLD",
    "LOAD_LOW_THRESHOLD",
    "RECENT_REVIEW_THRESHOLD",
    "WEIGHTS",
    "score",
]

# Thresholds are exposed as module constants so the rationale layer
# can re-use the exact same cutoffs when choosing its phrasing.
AUTHORSHIP_SHARE_THRESHOLD = 0.20
RECENT_REVIEW_THRESHOLD = 3
CALL_GRAPH_OVERLAP_THRESHOLD = 1
ACCEPTANCE_RATE_THRESHOLD = 0.70
LOAD_LOW_THRESHOLD = 3  # strictly less than
LOAD_HIGH_THRESHOLD = 8  # strictly greater than

# Per-signal weights. Positive signals sum to 1.00 so the max
# unpenalised score is 1.00. The load penalty can push a saturated
# candidate down to 0.80; we keep the final score unclamped so tests
# can inspect the raw number.
WEIGHTS: dict[str, float] = {
    "codeowner": 0.30,
    "authorship": 0.25,
    "recent_reviews": 0.15,
    "call_graph": 0.10,
    "acceptance": 0.10,
    "has_capacity": 0.10,
    "overloaded_penalty": -0.20,
}


def score(_candidate: Candidate, signals: Signals) -> float:
    """Return the reviewer-suitability score for one candidate.

    The ``_candidate`` argument is unused today but part of the public
    signature because future signals (e.g. "candidate is in the same
    organisation as the PR author") will need the :class:`Candidate`
    to look at attributes not carried in :class:`Signals`.
    """

    s = 0.0
    if signals.is_codeowner:
        s += WEIGHTS["codeowner"]
    if signals.authorship_share >= AUTHORSHIP_SHARE_THRESHOLD:
        s += WEIGHTS["authorship"]
    if signals.recent_review_count >= RECENT_REVIEW_THRESHOLD:
        s += WEIGHTS["recent_reviews"]
    if signals.call_graph_overlap >= CALL_GRAPH_OVERLAP_THRESHOLD:
        s += WEIGHTS["call_graph"]
    if signals.review_acceptance_rate >= ACCEPTANCE_RATE_THRESHOLD:
        s += WEIGHTS["acceptance"]
    if signals.open_pr_load < LOAD_LOW_THRESHOLD:
        s += WEIGHTS["has_capacity"]
    if signals.open_pr_load > LOAD_HIGH_THRESHOLD:
        s += WEIGHTS["overloaded_penalty"]
    return s
