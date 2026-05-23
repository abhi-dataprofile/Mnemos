"""Shared data types for the Reviewer Router.

Kept separate from :mod:`.router` so the scoring helpers don't have to
import the agent module (which would pull in the Pydantic models and
make these dataclasses harder to use from tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

__all__ = [
    "Candidate",
    "ScoredCandidate",
    "Signals",
]


@dataclass(slots=True, frozen=True)
class Candidate:
    """One person who might be asked to review the PR.

    ``person_id`` is optional: CODEOWNERS can surface user handles that
    don't exist in the graph yet (a new hire added to the ownership
    file before they've made their first commit). In that case we can
    still score them on the ``is_codeowner`` signal alone.

    ``is_team`` marks ``org/team`` CODEOWNERS entries. v0.1 does not
    expand teams to members — we surface the team handle verbatim and
    flag it. The formatter can render these differently (e.g. without
    the ``@-mention``) once a renderer update lands.
    """

    login: str
    person_id: UUID | None = None
    is_team: bool = False


@dataclass(slots=True)
class Signals:
    """All the raw signals the scoring function reads.

    Kept as a mutable dataclass so :func:`gather_signals` can set
    fields one at a time and individual signal failures don't corrupt
    the others. Defaults mean "signal unavailable" (zero / empty /
    False), which the scoring function treats as a neutral "no
    contribution" rather than a negative penalty.
    """

    is_codeowner: bool = False
    authorship_share: float = 0.0
    """Fraction of touched files the candidate has authored (0.0-1.0)."""

    recent_review_count: int = 0
    call_graph_overlap: int = 0
    review_acceptance_rate: float = 0.0
    """Approved reviews divided by total reviews over the window."""

    open_pr_load: int = 0
    """Open PRs authored by the candidate — a stand-in for review backlog."""

    total_reviews: int = 0
    """Raw review volume in the window. Used for signal-availability
    checks in the rationale layer."""

    authored_files: tuple[str, ...] = field(default_factory=tuple)
    """PR-touched files the candidate has authored in the window.
    Kept so the rationale layer can quote a specific file path."""


@dataclass(slots=True, frozen=True)
class ScoredCandidate:
    """A candidate after scoring. The agent ranks these and picks the top 3."""

    candidate: Candidate
    score: float
    signals: Signals
