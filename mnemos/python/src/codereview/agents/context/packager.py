"""ContextPackager agent.

Second agent. Assembles the 30-second briefing a reviewer reads before
the diff: related past PRs, ADRs in scope, recent commits on touched
files, linked issues, risk notes, and a short LLM-written narrative.

The agent is almost entirely deterministic graph queries. The one LLM
call (the summariser) can fail without killing the packet — we fall
back to a short, dependency-free narrative and keep going.

Wire surface:

- Emits zero :class:`Finding` objects. The Context Packager's output is
  the full context packet, not a list of problems, so stuffing it into
  a "finding" would mis-type the data.
- Attaches the structured packet to :attr:`AgentResult.metadata` under
  the ``context_packet`` key. The orchestration formatter recognises
  this key and routes the packet into the review payload's ``context``
  field rather than into ``conflicts``.

The :attr:`AgentContext.workspace_root` seam is *not* consumed by the
Context Packager — the packager reads from the graph and the PR
snapshot only, never from an on-disk checkout.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from codereview.agents.base import AgentContext, AgentResult, BaseAgent
from codereview.agents.context.adrs import find_relevant_adrs
from codereview.agents.context.history import fetch_recent_history
from codereview.agents.context.issues import find_linked_issues
from codereview.agents.context.related_prs import find_related_prs
from codereview.agents.context.risk import compute_risk_notes
from codereview.agents.context.summary import summarise_packet
from codereview.agents.context.types import ContextPacket
from codereview.logging import get_logger

__all__ = ["ContextPackager"]

_log = get_logger(__name__)


class ContextPackager(BaseAgent):
    """Assemble the reviewer context packet for a PR."""

    name: ClassVar[str] = "context_packager"
    description: ClassVar[str] = (
        "Assembles the context a reviewer needs before reading the diff: "
        "related past PRs, accepted ADRs, recent commits, linked issues, "
        "and neutral risk notes."
    )
    version: ClassVar[str] = "0.1.0"

    async def run(self, ctx: AgentContext) -> AgentResult:
        metadata: dict[str, Any] = {"sections": {}}

        pr_embedding = await _embed_pr(ctx, metadata)

        related_prs, prs_meta = await _related_prs(ctx, pr_embedding)
        metadata["sections"]["related_prs"] = prs_meta

        related_adrs, adrs_meta = await _related_adrs(ctx, pr_embedding)
        metadata["sections"]["related_adrs"] = adrs_meta

        recent_commits, hist_meta = await _recent_history(ctx)
        metadata["sections"]["recent_commits"] = hist_meta

        linked_issues, issues_meta = await _linked_issues(ctx)
        metadata["sections"]["linked_issues"] = issues_meta

        risk_notes, risk_meta = await _risk_notes(ctx)
        metadata["sections"]["risk_notes"] = risk_meta

        packet = ContextPacket(
            related_prs=related_prs,
            related_adrs=related_adrs,
            recent_commits=recent_commits,
            linked_issues=linked_issues,
            risk_notes=risk_notes,
            narrative="",
        )

        narrative, narrative_meta = await _summarise(ctx, packet)
        packet.narrative = narrative
        metadata["sections"]["narrative"] = narrative_meta

        metadata["context_packet"] = packet.to_wire()

        return AgentResult(
            agent_name=self.name,
            findings=[],
            metadata=metadata,
        )


# -- Section runners -------------------------------------------------------
#
# Each helper wraps one subsection of the packet assembly in try/except so a
# single failure doesn't lose the rest. All return a (value, metadata) pair
# so the agent can record what happened per section without cluttering the
# main method.


async def _embed_pr(
    ctx: AgentContext, metadata: dict[str, Any]
) -> list[float] | None:
    """Compute the prose embedding or return ``None`` when unavailable."""

    embed_fn = getattr(ctx.llm, "embed_prose", None)
    if embed_fn is None:
        metadata["embedding_skipped_reason"] = "embed_prose not configured"
        return None

    text = _pr_prose(ctx)
    try:
        vector = await embed_fn(text)
    except Exception as exc:
        metadata["embedding_skipped_reason"] = f"embed_prose failed: {exc!r}"
        _log.warning("context.embed_error", error=repr(exc))
        return None

    return list(vector) if vector is not None else None


async def _related_prs(
    ctx: AgentContext, embedding: list[float] | None
) -> tuple[list, dict[str, Any]]:
    meta: dict[str, Any] = {"count": 0}
    try:
        out = await find_related_prs(
            pr_embedding=embedding,
            pr_files={f.path for f in ctx.pr.changed_files},
            pr_author=ctx.pr.author,
            pr_number=ctx.pr.number,
            graph=ctx.graph,
            k=3,
        )
    except Exception as exc:
        meta["error"] = repr(exc)
        _log.warning("context.related_prs_error", error=repr(exc))
        return [], meta
    meta["count"] = len(out)
    return out, meta


async def _related_adrs(
    ctx: AgentContext, embedding: list[float] | None
) -> tuple[list, dict[str, Any]]:
    meta: dict[str, Any] = {"count": 0}
    try:
        out = await find_relevant_adrs(
            pr_embedding=embedding,
            graph=ctx.graph,
            k=5,
        )
    except Exception as exc:
        meta["error"] = repr(exc)
        _log.warning("context.related_adrs_error", error=repr(exc))
        return [], meta
    meta["count"] = len(out)
    return out, meta


async def _recent_history(ctx: AgentContext) -> tuple[list, dict[str, Any]]:
    meta: dict[str, Any] = {"count": 0}
    try:
        out = await fetch_recent_history(
            repo_id=ctx.repo_id,
            file_paths=[f.path for f in ctx.pr.changed_files],
            graph=ctx.graph,
        )
    except Exception as exc:
        meta["error"] = repr(exc)
        _log.warning("context.history_error", error=repr(exc))
        return [], meta
    meta["count"] = len(out)
    return out, meta


async def _linked_issues(ctx: AgentContext) -> tuple[list, dict[str, Any]]:
    meta: dict[str, Any] = {"count": 0}
    try:
        out = await find_linked_issues(
            repo_id=ctx.repo_id,
            pr_body=ctx.pr.body or "",
            graph=ctx.graph,
        )
    except Exception as exc:
        meta["error"] = repr(exc)
        _log.warning("context.issues_error", error=repr(exc))
        return [], meta
    meta["count"] = len(out)
    return out, meta


async def _risk_notes(ctx: AgentContext) -> tuple[list, dict[str, Any]]:
    meta: dict[str, Any] = {"count": 0}
    now = _now_utc()
    try:
        out = await compute_risk_notes(
            repo_id=ctx.repo_id,
            changed_files=ctx.pr.changed_files,
            graph=ctx.graph,
            now=now,
        )
    except Exception as exc:
        meta["error"] = repr(exc)
        _log.warning("context.risk_error", error=repr(exc))
        return [], meta
    meta["count"] = len(out)
    return out, meta


async def _summarise(
    ctx: AgentContext, packet: ContextPacket
) -> tuple[str, dict[str, Any]]:
    meta: dict[str, Any] = {"source": "llm"}
    try:
        narrative = await summarise_packet(llm=ctx.llm, pr=ctx.pr, packet=packet)
    except Exception as exc:
        meta["source"] = "fallback"
        meta["error"] = repr(exc)
        _log.warning("context.summary_error", error=repr(exc))
        return "", meta
    if not narrative:
        meta["source"] = "empty"
    return narrative, meta


# -- Utilities -------------------------------------------------------------


def _pr_prose(ctx: AgentContext) -> str:
    """Prose to embed: title + body, stripped of nothing."""

    body = (ctx.pr.body or "").strip()
    if body:
        return f"{ctx.pr.title}\n\n{body}"
    return ctx.pr.title


def _now_utc() -> dt.datetime:
    """Timezone-aware ``now`` — isolated in a helper so tests can patch it."""

    # Use the 3.10-compatible spelling so the dev sandbox (3.10) still runs;
    # project targets 3.11 in CI but tests in some environments run on 3.10.
    return dt.datetime.now(tz=dt.timezone.utc)  # noqa: UP017
