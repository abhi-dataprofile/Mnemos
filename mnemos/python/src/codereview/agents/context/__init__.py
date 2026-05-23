"""Context Packager agent and its pure-Python support modules.

Public surface:

- :class:`ContextPackager` — the agent itself
- :class:`ContextPacket` and its sub-types — the packet shape returned
  in :attr:`AgentResult.metadata` and consumed by the orchestration
  formatter

Downstream code should prefer the re-exports here over importing from
the internal submodules.
"""

from __future__ import annotations

from codereview.agents.context.packager import ContextPackager
from codereview.agents.context.types import (
    ContextPacket,
    LinkedIssue,
    RecentCommit,
    RelatedADR,
    RelatedPR,
)

__all__ = [
    "ContextPackager",
    "ContextPacket",
    "LinkedIssue",
    "RecentCommit",
    "RelatedADR",
    "RelatedPR",
]
