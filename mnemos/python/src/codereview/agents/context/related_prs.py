"""Related PR finder.

Given the current PR's embedding, its touched-file set, and author, return
up to ``k`` past PRs that are plausibly useful background. Candidates are
pulled from :meth:`GraphClient.similar_prs_scored` (a larger pool than we
keep), re-scored locally as ``0.6 * similarity + 0.4 * jaccard``, then
filtered to drop the author's own prior PRs unless the file overlap is
high — the "you *are* the expert on this file" escape hatch from the
plan doc.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from codereview.agents.context.types import RelatedPR
from codereview.logging import get_logger

__all__ = [
    "find_related_prs",
    "jaccard",
]


_log = get_logger(__name__)

# Default pool size when fetching candidates. Larger than ``k`` so the
# re-rank has something to work with; too large and we pay extra Jaccard
# lookups without much gain.
_DEFAULT_POOL = 10

# Minimum final score to surface a PR at all. Dependency bumps and other
# generic PRs often land in the similarity pool with no real relevance;
# this floor keeps them from dominating the output.
_MIN_SCORE = 0.15

# File overlap threshold that overrides the "exclude own PRs" filter.
# Calibrated with the plan doc's guidance ("sometimes you are legitimately
# the expert on one file"). Jaccard of 0.8+ means near-identical file set.
_AUTHOR_OVERRIDE_OVERLAP = 0.8

_SIM_WEIGHT = 0.6
_JACCARD_WEIGHT = 0.4


async def find_related_prs(
    *,
    pr_embedding: Sequence[float] | None,
    pr_files: set[str],
    pr_author: str | None,
    pr_number: int,
    graph: Any,
    k: int = 3,
    pool: int = _DEFAULT_POOL,
) -> list[RelatedPR]:
    """Return up to ``k`` related past PRs, sorted by blended score desc.

    Parameters
    ----------
    pr_embedding:
        Prose embedding of the current PR's title + body. When ``None``
        (the embedder was not available), returns an empty list — the
        packet gracefully degrades to no related PRs rather than raising.
    pr_files:
        Paths of files changed by the current PR. Used for Jaccard.
    pr_author:
        GitHub login of the current PR's author. Their past PRs are
        excluded unless the file overlap exceeds
        :data:`_AUTHOR_OVERRIDE_OVERLAP`.
    pr_number:
        The current PR's number, excluded from results so it can't
        recommend itself (this matters when the graph has already been
        re-indexed with the open PR's embedding in it).
    graph:
        :class:`GraphClient` or a test fake with the same method surface
        (``similar_prs_scored``, ``files_touched_by_pr``).
    k:
        Maximum number of PRs to return.
    pool:
        How many candidates to fetch from the graph before re-ranking.
    """

    if pr_embedding is None:
        return []

    similar_fn = getattr(graph, "similar_prs_scored", None)
    files_fn = getattr(graph, "files_touched_by_pr", None)
    if similar_fn is None or files_fn is None:
        _log.warning("context.related_prs.missing_graph_methods")
        return []

    try:
        candidates: list[tuple[Any, float]] = list(
            await similar_fn(list(pr_embedding), k=pool)
        )
    except Exception as exc:
        _log.warning("context.related_prs.similarity_error", error=repr(exc))
        return []

    scored: list[RelatedPR] = []
    for ref, similarity in candidates:
        number = getattr(ref, "number", None)
        if number == pr_number:
            continue

        try:
            candidate_files = set(await files_fn(ref.id))
        except Exception as exc:
            _log.warning(
                "context.related_prs.files_error",
                pr_id=str(getattr(ref, "id", "?")),
                error=repr(exc),
            )
            candidate_files = set()

        overlap = jaccard(pr_files, candidate_files)

        if pr_author and getattr(ref, "author_login", None) == pr_author:
            if overlap < _AUTHOR_OVERRIDE_OVERLAP:
                continue

        score = _SIM_WEIGHT * max(0.0, similarity) + _JACCARD_WEIGHT * overlap
        if score < _MIN_SCORE:
            continue

        scored.append(
            RelatedPR(
                number=number or 0,
                title=getattr(ref, "title", "") or "",
                url=None,
                score=score,
            )
        )

    scored.sort(key=lambda pr: pr.score, reverse=True)
    return scored[:k]


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity of two sets; 0 when both are empty."""

    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)
