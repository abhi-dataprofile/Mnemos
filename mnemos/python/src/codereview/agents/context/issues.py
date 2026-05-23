"""Linked-issue parser for the Context Packager.

Three patterns are recognised in the PR body:

1. ``(fixes|closes|resolves) #N`` — same-repo GitHub issue. We look up
   its title and state via the graph; failures degrade to a bare
   reference rather than dropping it.
2. ``(fixes|closes|resolves) ACME-123`` — external tracker (Linear,
   Jira, etc.). We surface the identifier and do not chase it.
3. Bare ``ACME-123`` (or any uppercase-prefix token of the form
   ``[A-Z][A-Z0-9]+-\\d+``) anywhere in the body — also external.

The parser intentionally rejects free-floating numeric tokens (no ``#``,
no prefix). Random version numbers and counts in PR bodies otherwise
turn into a flood of false-positive issue links — see plan §risks.

Patterns are case-insensitive on the verb but case-sensitive on the
identifier prefix to avoid catching ordinary words.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from codereview.agents.context.types import LinkedIssue
from codereview.logging import get_logger

__all__ = [
    "find_linked_issues",
    "parse_pr_body",
]

_log = get_logger(__name__)

# (fixes|closes|resolves|fix|close|resolve) #123
_GITHUB_RE = re.compile(
    r"\b(?:fix(?:e[sd])?|clos(?:e[sd])?|resolv(?:e[sd])?)\s*[:\-]?\s*#(\d+)\b",
    re.IGNORECASE,
)

# (fixes|...) ACME-123 — explicit linking verb in front of an external id.
_EXTERNAL_VERB_RE = re.compile(
    r"\b(?:fix(?:e[sd])?|clos(?:e[sd])?|resolv(?:e[sd])?)\s*[:\-]?\s*"
    r"([A-Z][A-Z0-9]+-\d+)\b",
    re.IGNORECASE,
)

# Bare ACME-123 anywhere. The prefix must be at least 2 uppercase chars
# so we don't catch single-letter identifiers ("A-1") and similar noise.
# Case-sensitive on the prefix so ordinary words don't match.
_EXTERNAL_BARE_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


async def find_linked_issues(
    *,
    repo_id: UUID,
    pr_body: str,
    graph: Any,
) -> list[LinkedIssue]:
    """Parse ``pr_body`` and enrich same-repo issues from the graph."""

    parsed = parse_pr_body(pr_body)
    if not parsed:
        return []

    issue_lookup = getattr(graph, "issue_by_number", None)

    out: list[LinkedIssue] = []
    for item in parsed:
        if item.kind == "github" and item.number is not None and issue_lookup is not None:
            try:
                row = await issue_lookup(repo_id, item.number)
            except Exception as exc:
                _log.warning(
                    "context.issues.lookup_error",
                    number=item.number,
                    error=repr(exc),
                )
                row = None
            if row is not None:
                _, title, state = row
                out.append(
                    LinkedIssue(
                        kind="github",
                        identifier=item.identifier,
                        number=item.number,
                        title=title,
                        state=state,
                        url=None,
                    )
                )
                continue
        out.append(item)
    return out


def parse_pr_body(body: str) -> list[LinkedIssue]:
    """Pure-function pass over ``body``. No graph access.

    Splitting parsing from enrichment makes the regex behaviour trivially
    unit-testable and lets the agent run a "what would we link?" preview
    without touching the DB.
    """

    if not body:
        return []

    seen: set[tuple[str, str]] = set()
    out: list[LinkedIssue] = []

    for match in _GITHUB_RE.finditer(body):
        number = int(match.group(1))
        identifier = f"#{number}"
        key = ("github", identifier)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            LinkedIssue(
                kind="github",
                identifier=identifier,
                number=number,
            )
        )

    # Two passes for external: explicit verb prefix first (so they are
    # surfaced even if the bare regex didn't match), then bare matches.
    for match in _EXTERNAL_VERB_RE.finditer(body):
        identifier = match.group(1)
        key = ("external", identifier)
        if key in seen:
            continue
        seen.add(key)
        out.append(LinkedIssue(kind="external", identifier=identifier))

    for match in _EXTERNAL_BARE_RE.finditer(body):
        identifier = match.group(1)
        key = ("external", identifier)
        if key in seen:
            continue
        seen.add(key)
        out.append(LinkedIssue(kind="external", identifier=identifier))

    return out
