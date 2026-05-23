"""Packet summariser — the single LLM call in the Context Packager.

Takes the assembled packet data, renders it into the versioned prompt,
and asks the LLM for one 50-80 word paragraph the reviewer reads before
the diff. On any error the summariser degrades to a short,
deterministic fallback string so the packet still has a narrative field
populated (or to the empty string when the packet itself is empty —
there is nothing useful to say).
"""

from __future__ import annotations

from typing import Any

from codereview.agents.base import PullRequestSnapshot
from codereview.agents.context.prompts import ContextSummary
from codereview.agents.context.types import (
    ContextPacket,
    LinkedIssue,
    RecentCommit,
    RelatedADR,
    RelatedPR,
)
from codereview.llm.prompts import load_prompt
from codereview.logging import get_logger

__all__ = ["summarise_packet"]

_log = get_logger(__name__)

# Loaded eagerly — a typo or rename in the prompt file makes the agent
# module fail to import, which surfaces as a loud test failure.
_PROMPT = load_prompt("context_summary", "v1")

_DEFAULT_MAX_TOKENS = 300


async def summarise_packet(
    *,
    llm: Any,
    pr: PullRequestSnapshot,
    packet: ContextPacket,
) -> str:
    """Render the packet into a reviewer-facing paragraph.

    Returns ``""`` when the packet has nothing worth summarising. On
    LLM errors returns a deterministic single-line fallback rather than
    raising — the coordinator treats a Context Packager crash as a
    review-level failure, and we'd rather ship a partial packet than
    skip the agent entirely.
    """

    if packet.is_empty():
        return ""

    structured_call = getattr(llm, "structured_call", None)
    if structured_call is None:
        return _deterministic_fallback(packet)

    rendered = _PROMPT.render(
        {
            "pr_title": pr.title,
            "pr_body": pr.body or "(no description)",
            "related_prs": _format_prs(packet.related_prs),
            "related_adrs": _format_adrs(packet.related_adrs),
            "recent_commits": _format_commits(packet.recent_commits),
            "linked_issues": _format_issues(packet.linked_issues),
            "risk_notes": _format_notes(packet.risk_notes),
        }
    )

    try:
        result: ContextSummary = await structured_call(
            prompt=rendered,
            output_schema=ContextSummary,
            prompt_version=_PROMPT.prompt_version,
            system=_PROMPT.system,
            max_tokens=_DEFAULT_MAX_TOKENS,
        )
    except Exception as exc:
        _log.warning("context.summary.llm_error", error=repr(exc))
        return _deterministic_fallback(packet)

    return result.summary.strip()


# -- Rendering helpers -----------------------------------------------------


def _format_prs(prs: list[RelatedPR]) -> str:
    if not prs:
        return "(none)"
    return "\n".join(f"- #{p.number}: {p.title}" for p in prs)


def _format_adrs(adrs: list[RelatedADR]) -> str:
    if not adrs:
        return "(none)"
    return "\n".join(f"- {a.title}" for a in adrs)


def _format_commits(commits: list[RecentCommit]) -> str:
    if not commits:
        return "(none)"
    lines: list[str] = []
    for c in commits:
        short = (c.sha or "")[:7] or "(no sha)"
        title = c.title or "(no message)"
        suffix = f" — {c.file_path}" if c.file_path else ""
        lines.append(f"- {short} {title}{suffix}")
    return "\n".join(lines)


def _format_issues(issues: list[LinkedIssue]) -> str:
    if not issues:
        return "(none)"
    lines: list[str] = []
    for i in issues:
        suffix_bits: list[str] = []
        if i.title:
            suffix_bits.append(i.title)
        if i.state:
            suffix_bits.append(f"state={i.state}")
        suffix = f" — {'; '.join(suffix_bits)}" if suffix_bits else ""
        lines.append(f"- [{i.kind}] {i.identifier}{suffix}")
    return "\n".join(lines)


def _format_notes(notes: list[str]) -> str:
    if not notes:
        return "(none)"
    return "\n".join(f"- {n}" for n in notes)


def _deterministic_fallback(packet: ContextPacket) -> str:
    """Short, dependency-free narrative when the LLM call failed."""

    parts: list[str] = []
    if packet.related_prs:
        parts.append(f"{len(packet.related_prs)} related PR(s)")
    if packet.related_adrs:
        parts.append(f"{len(packet.related_adrs)} accepted ADR(s)")
    if packet.recent_commits:
        parts.append(f"{len(packet.recent_commits)} recent commit(s)")
    if packet.linked_issues:
        parts.append(f"{len(packet.linked_issues)} linked issue(s)")
    if packet.risk_notes:
        parts.append(f"{len(packet.risk_notes)} risk note(s)")
    if not parts:
        return ""
    return "Packet assembled: " + ", ".join(parts) + "."
