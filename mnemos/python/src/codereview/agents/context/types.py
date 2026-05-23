"""Data types shared across Context Packager helpers.

The Context Packager assembles a :class:`ContextPacket` that travels on the
review wire as the ``context`` field (see
``typescript/src/formatters/reviewComment.ts``'s ``ContextPacket``). The
Python side owns a slightly richer shape than the TS renderer currently
consumes — ``linked_issues`` and ``risk_notes`` are Python-only for v0.1
and will be picked up in a TS follow-up. Unknown fields are ignored on the
TS side so the extra keys are wire-safe.

Everything here is a plain dataclass because these types never cross the
LLM surface — only ``ContextSummary`` (defined in :mod:`.summary`) does.
Pydantic would buy nothing here and complicate the ``model_dump`` path
the formatter uses to emit JSON.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

__all__ = [
    "ContextPacket",
    "LinkedIssue",
    "RecentCommit",
    "RelatedADR",
    "RelatedPR",
]


@dataclass(slots=True, frozen=True)
class RelatedPR:
    """A past PR surfaced as relevant background for the current one."""

    number: int
    title: str
    url: str | None = None
    score: float = 0.0
    """Blended similarity + file-overlap score. Not serialised to the wire."""


@dataclass(slots=True, frozen=True)
class RelatedADR:
    """An accepted ADR the reviewer should keep in mind."""

    title: str
    url: str | None = None


@dataclass(slots=True, frozen=True)
class RecentCommit:
    """One recent commit that touched a PR-changed file.

    ``file_path`` records which of the PR's files this commit was
    attached to; the same SHA can appear once per touched file.
    """

    sha: str
    title: str | None = None
    url: str | None = None
    author_login: str | None = None
    file_path: str | None = None


IssueKind = Literal["github", "external"]


@dataclass(slots=True, frozen=True)
class LinkedIssue:
    """An issue or ticket linked from the PR body.

    ``kind="github"`` means a same-repo issue number and is enriched with
    the stored title/state when present in the graph; ``kind="external"``
    carries only the bare identifier (e.g. ``"ACME-123"``). The PR body
    parser does not chase external trackers — that is Phase 8+ work.
    """

    kind: IssueKind
    identifier: str
    number: int | None = None
    title: str | None = None
    state: str | None = None
    url: str | None = None


@dataclass(slots=True)
class ContextPacket:
    """The full Context Packager output."""

    related_prs: list[RelatedPR] = field(default_factory=list)
    related_adrs: list[RelatedADR] = field(default_factory=list)
    recent_commits: list[RecentCommit] = field(default_factory=list)
    linked_issues: list[LinkedIssue] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    narrative: str = ""

    def is_empty(self) -> bool:
        """True when nothing worth rendering was assembled."""

        return not (
            self.related_prs
            or self.related_adrs
            or self.recent_commits
            or self.linked_issues
            or self.risk_notes
            or self.narrative.strip()
        )

    def to_wire(self) -> dict[str, object]:
        """Serialise to the review-payload ``context`` shape.

        The internal :attr:`RelatedPR.score` is dropped. Empty lists are
        omitted so the TS renderer's ``(list ?? []).length > 0`` checks
        do not treat them as present.
        """

        out: dict[str, object] = {}
        if self.related_prs:
            out["related_prs"] = [
                {k: v for k, v in asdict(pr).items() if k != "score" and v is not None}
                for pr in self.related_prs
            ]
        if self.related_adrs:
            out["related_adrs"] = [
                {k: v for k, v in asdict(adr).items() if v is not None}
                for adr in self.related_adrs
            ]
        if self.recent_commits:
            out["recent_commits"] = [
                {k: v for k, v in asdict(c).items() if v is not None}
                for c in self.recent_commits
            ]
        if self.linked_issues:
            out["linked_issues"] = [
                {k: v for k, v in asdict(i).items() if v is not None}
                for i in self.linked_issues
            ]
        if self.risk_notes:
            out["risk_notes"] = list(self.risk_notes)
        if self.narrative.strip():
            out["narrative"] = self.narrative.strip()
        return out
