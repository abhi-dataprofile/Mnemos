"""Reviewer Router agent and its pure-Python support modules.

Public surface:

- :class:`ReviewerRouter` — the agent itself
- :class:`Candidate`, :class:`Signals`, :class:`ScoredCandidate` — the
  shared types used by scoring and the formatter

Downstream code should prefer the re-exports here over importing from
the internal submodules.
"""

from __future__ import annotations

from codereview.agents.router.router import ReviewerRouter
from codereview.agents.router.types import Candidate, ScoredCandidate, Signals

__all__ = [
    "Candidate",
    "ReviewerRouter",
    "ScoredCandidate",
    "Signals",
]
