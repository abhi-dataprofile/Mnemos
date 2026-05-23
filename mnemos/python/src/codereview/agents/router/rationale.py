"""Rationale generation for the Reviewer Router.

For each top-3 candidate we want a one-sentence explanation of *why*
the router picked them. No LLM — just a priority list of phrasings
keyed off which signals fired. The first rule whose precondition
matches wins, so heavier signals (CODEOWNERS match, deep authorship)
are tried before lighter ones (mere capacity).

Keeping this deterministic has two benefits: (1) Phase 6 doesn't add
another LLM call to the hot path, and (2) the phrasing is stable so
repeated runs produce the same rationale — reviewers who see the bot
comment across multiple PRs won't get whiplash from it rephrasing
itself.
"""

from __future__ import annotations

from codereview.agents.router.score import (
    ACCEPTANCE_RATE_THRESHOLD,
    AUTHORSHIP_SHARE_THRESHOLD,
    CALL_GRAPH_OVERLAP_THRESHOLD,
    LOAD_LOW_THRESHOLD,
    RECENT_REVIEW_THRESHOLD,
)
from codereview.agents.router.types import Candidate, Signals

__all__ = ["rationale"]


def rationale(candidate: Candidate, signals: Signals) -> str:
    """One sentence explaining why ``candidate`` is a good reviewer.

    Order of precedence (strongest signal wins):

    1. CODEOWNERS match → name the owned path when we have one.
    2. Deep authorship → "authored X% of touched files".
    3. Call-graph overlap → "has modified adjacent code recently".
    4. High-volume reviewer with a good acceptance rate.
    5. Simple recent-review volume.
    6. "Currently has capacity" fallback.
    7. Absolute fallback: the candidate surfaced at all, even without
       any firing signal. Rare but possible for pure CODEOWNERS team
       entries.
    """

    if signals.is_codeowner:
        path = _codeowner_path_hint(signals)
        if path is not None:
            return f"CODEOWNERS match for {path}."
        return "CODEOWNERS match for the files touched by this PR."

    if signals.authorship_share >= AUTHORSHIP_SHARE_THRESHOLD:
        pct = round(signals.authorship_share * 100)
        return f"Authored {pct}% of touched files in the last 6 months."

    if signals.call_graph_overlap >= CALL_GRAPH_OVERLAP_THRESHOLD:
        count = signals.call_graph_overlap
        noun = "symbol" if count == 1 else "symbols"
        return f"Recently modified {count} {noun} that call into this PR's code."

    if (
        signals.recent_review_count >= RECENT_REVIEW_THRESHOLD
        and signals.total_reviews > 0
        and signals.review_acceptance_rate >= ACCEPTANCE_RATE_THRESHOLD
    ):
        return (
            f"Reviewed {signals.recent_review_count} PRs in this area with a "
            f"{round(signals.review_acceptance_rate * 100)}% acceptance rate."
        )

    if signals.recent_review_count >= RECENT_REVIEW_THRESHOLD:
        return f"Reviewed {signals.recent_review_count} PRs touching these files recently."

    if signals.open_pr_load < LOAD_LOW_THRESHOLD:
        load = signals.open_pr_load
        noun = "PR" if load == 1 else "PRs"
        return f"Currently has capacity ({load} open {noun})."

    if candidate.is_team:
        return f"Listed as a team owner ({candidate.login})."

    return "Surfaced by historical review activity near this PR."


# -- Internals -------------------------------------------------------------


def _codeowner_path_hint(signals: Signals) -> str | None:
    """Pick one PR-touched file path to name in the CODEOWNERS rationale.

    Prefers a file the candidate has actually authored (their direct
    stake) over an arbitrary touched file. When neither is available
    we return ``None`` and the caller falls back to a generic phrasing.
    """

    if signals.authored_files:
        return signals.authored_files[0]
    return None
