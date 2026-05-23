"""ADR retriever for the Context Packager.

Reuses :meth:`GraphClient.similar_adrs` — the same call the Conflict
Detector makes — but keeps only accepted ADRs and surfaces them as
background rather than checking them for contradiction. A single ADR can
appear in both agents' outputs; the orchestration formatter dedupes
cross-agent findings on ``(kind, title, first-path)`` but the Context
Packager's ADR list lives in its own wire field, so there is no overlap
in practice.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from codereview.agents.context.types import RelatedADR
from codereview.logging import get_logger

__all__ = ["find_relevant_adrs"]

_log = get_logger(__name__)


async def find_relevant_adrs(
    *,
    pr_embedding: Sequence[float] | None,
    graph: Any,
    k: int = 5,
) -> list[RelatedADR]:
    """Return up to ``k`` accepted ADRs closest to ``pr_embedding``.

    Gracefully degrades when the embedder is not available (embedding
    is ``None``) or the graph client lacks :meth:`similar_adrs`.
    """

    if pr_embedding is None:
        return []

    similar_fn = getattr(graph, "similar_adrs", None)
    if similar_fn is None:
        return []

    try:
        candidates = await similar_fn(list(pr_embedding), k=k)
    except Exception as exc:
        _log.warning("context.adrs.similarity_error", error=repr(exc))
        return []

    out: list[RelatedADR] = []
    for adr in candidates:
        if getattr(adr, "status", None) != "accepted":
            continue
        title = getattr(adr, "title", None)
        if not title:
            continue
        out.append(RelatedADR(title=title, url=None))
    return out
